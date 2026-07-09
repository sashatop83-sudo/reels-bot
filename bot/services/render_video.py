import shutil
import subprocess
from pathlib import Path

from bot.services.ffmpeg_utils import (
    get_ffmpeg_binary,
    get_media_duration,
    get_video_size,
    has_audio_stream,
)
from bot.services.subtitles import (
    DEFAULT_COLOR,
    DEFAULT_FONT,
    DEFAULT_POSITION,
    DEFAULT_SIZE,
    DEFAULT_STYLE,
    FONTS_DIR,
    build_ass,
)
from bot.services.transcribe import SubtitleSegment

TARGET_W = 1080
TARGET_H = 1920
MAX_SIDE = 1920  # не грузим Railway 4K-энкодом


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _needs_vertical_fit(width: int, height: int) -> bool:
    if height == 0:
        return False
    return width > height * 1.05


def _pick_preview_ts(segments: list[SubtitleSegment], video_path: Path) -> float:
    duration = get_media_duration(str(video_path))
    if not segments:
        return min(0.5, max(duration * 0.25, 0.1))
    pool = [s for s in segments if s.text.strip() and s.end <= duration * 0.9]
    if not pool:
        pool = segments
    best = max(pool, key=lambda s: len(s.text))
    ts = best.start + max((best.end - best.start) * 0.45, 0.2)
    return min(max(ts, 0.1), max(duration - 0.2, 0.2))


def _scale_down_filter(width: int, height: int) -> str:
    """Уменьшить 4K → 1080p, чтобы не убить память на Railway."""
    if max(width, height) <= MAX_SIDE:
        return ""
    return (
        "scale=w='if(gt(iw,ih),min(1920,iw),-2)'"
        ":h='if(gt(ih,iw),min(1920,ih),-2)'"
    )


def _build_video_filter(
    ass_relpath: str, vertical_fit: bool, width: int, height: int
) -> tuple[str, str, int, int]:
    fonts_dir = _escape_filter_path(str(FONTS_DIR))
    ass_filter = f"ass={ass_relpath}:fontsdir={fonts_dir}"
    downscale = _scale_down_filter(width, height)

    if vertical_fit:
        parts = [
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=12:1,eq=brightness=-0.08[bg]",
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]",
            f"[base]{ass_filter}[v]",
        ]
        return ";".join(parts), "complex", TARGET_W, TARGET_H

    if downscale:
        vf = f"{downscale},{ass_filter}"
        new_w = width if width <= height else min(width, MAX_SIDE)
        new_h = height if height <= width else min(height, MAX_SIDE)
        if width > height:
            new_w, new_h = MAX_SIDE, int(height * MAX_SIDE / width)
        else:
            new_h, new_w = MAX_SIDE, int(width * MAX_SIDE / height)
        return vf, "vf", new_w, new_h

    return ass_filter, "vf", width, height


def _ffmpeg_error(stderr: str, returncode: int) -> str:
    text = (stderr or "").strip()
    if returncode < 0:
        sig = -returncode
        if sig == 9:
            return "Серверу не хватило памяти при рендере. Попробуй видео покороче или меньше по разрешению."
        return f"FFmpeg остановлен сигналом {sig}."
    for line in text.splitlines():
        low = line.lower()
        if any(k in low for k in ("error", "invalid", "failed", "no such", "signal", "killed")):
            return line.strip()[:500]
    tail = text[-800:] if text else "неизвестная ошибка"
    return tail


def _run_ffmpeg(args: list[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка ffmpeg: {_ffmpeg_error(result.stderr, result.returncode)}")


def _prepare(
    video_path: Path,
    segments: list[SubtitleSegment],
    work_dir: Path,
    style_key: str,
    font_key: str,
    position_key: str,
    color_key: str,
    size_key: str,
    for_preview: bool = False,
    preview_ts: float = 0.0,
) -> tuple[str, str, bool]:
    local_input = work_dir / "input.mp4"
    ass_path = work_dir / "subs.ass"
    shutil.copy2(video_path, local_input)

    width, height = get_video_size(str(local_input))
    vertical_fit = _needs_vertical_fit(width, height)
    filter_expr, mode, canvas_w, canvas_h = _build_video_filter(
        "subs.ass", vertical_fit, width, height
    )

    ass_text = build_ass(
        segments, style_key, font_key, position_key, color_key,
        canvas_w, canvas_h, size_key, for_preview=for_preview, preview_ts=preview_ts,
    )
    ass_path.write_text(ass_text, encoding="utf-8")
    return filter_expr, mode, vertical_fit


def _encode_args(output_name: str, lite: bool = False) -> list[str]:
    if lite:
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-threads", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "4096",
            output_name,
        ]
    return [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-threads", "2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "4096",
        output_name,
    ]


def _build_render_command(
    work_dir: Path,
    filter_expr: str,
    mode: str,
    output_name: str,
    lite: bool,
) -> list[str]:
    ffmpeg_bin = get_ffmpeg_binary()
    audio = has_audio_stream(str(work_dir / "input.mp4"))

    args = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", "input.mp4"]
    if mode == "complex":
        args += ["-filter_complex", filter_expr, "-map", "[v]"]
    else:
        args += ["-vf", filter_expr, "-map", "0:v"]

    if audio:
        args += ["-map", "0:a?"]
        if lite:
            args += ["-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-af", "dynaudnorm", "-c:a", "aac", "-b:a", "128k"]

    args += _encode_args(output_name, lite=lite)
    return args


def render_video_with_subtitles(
    video_path: Path,
    segments: list[SubtitleSegment],
    output_path: Path,
    style_key: str = DEFAULT_STYLE,
    font_key: str = DEFAULT_FONT,
    position_key: str = DEFAULT_POSITION,
    color_key: str = DEFAULT_COLOR,
    size_key: str = DEFAULT_SIZE,
) -> Path:
    work_dir = output_path.parent
    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key, size_key
    )

    last_err: Exception | None = None
    for lite in (False, True):
        try:
            args = _build_render_command(work_dir, filter_expr, mode, output_path.name, lite=lite)
            _run_ffmpeg(args, work_dir)
            return output_path
        except RuntimeError as exc:
            last_err = exc
            if lite:
                break

    raise RuntimeError(str(last_err) if last_err else "Ошибка рендера")


def render_preview_image(
    video_path: Path,
    segments: list[SubtitleSegment],
    output_path: Path,
    style_key: str = DEFAULT_STYLE,
    font_key: str = DEFAULT_FONT,
    position_key: str = DEFAULT_POSITION,
    color_key: str = DEFAULT_COLOR,
    size_key: str = DEFAULT_SIZE,
) -> Path:
    ffmpeg_bin = get_ffmpeg_binary()
    work_dir = output_path.parent

    ts = _pick_preview_ts(segments, work_dir / "input.mp4")

    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key, size_key,
        for_preview=True, preview_ts=ts,
    )

    args = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{ts:.3f}", "-i", "input.mp4"]
    if mode == "complex":
        args += ["-filter_complex", filter_expr, "-map", "[v]"]
    else:
        args += ["-vf", filter_expr]
    args += ["-frames:v", "1", "-update", "1", output_path.name]

    _run_ffmpeg(args, work_dir)
    return output_path

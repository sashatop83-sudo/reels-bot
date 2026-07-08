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
AUDIO_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _needs_vertical_fit(width: int, height: int) -> bool:
    """9:16-обёртка только для горизонтального видео. Портрет не трогаем."""
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


def _build_video_filter(ass_relpath: str, vertical_fit: bool) -> tuple[str, str, int, int]:
    fonts_dir = _escape_filter_path(str(FONTS_DIR))
    ass_filter = f"ass={ass_relpath}:fontsdir={fonts_dir}"

    if vertical_fit:
        complex_filter = (
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=24:2,eq=brightness=-0.08[bg];"
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
            f"[base]{ass_filter}[v]"
        )
        return complex_filter, "complex", TARGET_W, TARGET_H

    return ass_filter, "vf", 0, 0


def _run_ffmpeg(args: list[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        tail = details[-1200:] if details else "ffmpeg вернул ошибку"
        raise RuntimeError(f"Ошибка ffmpeg: {tail}")


def _prepare(
    video_path: Path,
    segments: list[SubtitleSegment],
    work_dir: Path,
    style_key: str,
    font_key: str,
    position_key: str,
    color_key: str,
    size_key: str,
) -> tuple[str, str, bool]:
    local_input = work_dir / "input.mp4"
    ass_path = work_dir / "subs.ass"
    shutil.copy2(video_path, local_input)

    width, height = get_video_size(str(local_input))
    vertical_fit = _needs_vertical_fit(width, height)
    canvas_w, canvas_h = (TARGET_W, TARGET_H) if vertical_fit else (width, height)

    ass_text = build_ass(segments, style_key, font_key, position_key, color_key, canvas_w, canvas_h, size_key)
    ass_path.write_text(ass_text, encoding="utf-8")

    filter_expr, mode, _, _ = _build_video_filter("subs.ass", vertical_fit)
    return filter_expr, mode, vertical_fit


def _encode_args(output_name: str) -> list[str]:
    return [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_name,
    ]


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
    ffmpeg_bin = get_ffmpeg_binary()
    work_dir = output_path.parent

    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key, size_key
    )

    audio = has_audio_stream(str(work_dir / "input.mp4"))

    args = [ffmpeg_bin, "-y", "-i", "input.mp4"]
    if mode == "complex":
        args += ["-filter_complex", filter_expr, "-map", "[v]"]
        if audio:
            args += ["-map", "0:a?"]
    else:
        args += ["-vf", filter_expr]

    if audio:
        args += ["-af", AUDIO_FILTER, "-c:a", "aac", "-b:a", "192k"]

    args += _encode_args(output_path.name)
    _run_ffmpeg(args, work_dir)
    return output_path


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

    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key, size_key
    )

    ts = _pick_preview_ts(segments, work_dir / "input.mp4")

    args = [ffmpeg_bin, "-y", "-ss", f"{ts:.3f}", "-i", "input.mp4"]
    if mode == "complex":
        args += ["-filter_complex", filter_expr, "-map", "[v]"]
    else:
        args += ["-vf", filter_expr]
    args += ["-frames:v", "1", "-update", "1", output_path.name]

    _run_ffmpeg(args, work_dir)
    return output_path

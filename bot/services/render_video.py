import shutil
import subprocess
from pathlib import Path

from bot.services.ffmpeg_utils import (
    get_ffmpeg_binary,
    get_video_size,
    has_audio_stream,
)
from bot.services.subtitles import (
    DEFAULT_COLOR,
    DEFAULT_FONT,
    DEFAULT_POSITION,
    DEFAULT_STYLE,
    FONTS_DIR,
    build_ass,
)
from bot.services.transcribe import SubtitleSegment

TARGET_W = 1080
TARGET_H = 1920
VERTICAL_FIT_THRESHOLD = 0.65  # aspect w/h выше этого → добавляем вертикальный фон
AUDIO_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _needs_vertical_fit(width: int, height: int) -> bool:
    if height == 0:
        return False
    return (width / height) > VERTICAL_FIT_THRESHOLD


def _build_video_filter(ass_relpath: str, vertical_fit: bool) -> tuple[str, str, int, int]:
    """Возвращает (filter_expr, mode, canvas_w, canvas_h). mode: 'vf' или 'complex'."""
    fonts_dir = _escape_filter_path(str(FONTS_DIR))
    ass_filter = f"ass={ass_relpath}:fontsdir={fonts_dir}"

    if vertical_fit:
        complex_filter = (
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=24:2,eq=brightness=-0.08[bg];"
            f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{ass_filter}[v]"
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
) -> tuple[str, str, bool]:
    local_input = work_dir / "input.mp4"
    ass_path = work_dir / "subs.ass"
    shutil.copy2(video_path, local_input)

    width, height = get_video_size(str(local_input))
    vertical_fit = _needs_vertical_fit(width, height)
    canvas_w, canvas_h = (TARGET_W, TARGET_H) if vertical_fit else (width, height)

    ass_text = build_ass(segments, style_key, font_key, position_key, color_key, canvas_w, canvas_h)
    ass_path.write_text(ass_text, encoding="utf-8")

    filter_expr, mode, _, _ = _build_video_filter("subs.ass", vertical_fit)
    return filter_expr, mode, vertical_fit


def render_video_with_subtitles(
    video_path: Path,
    segments: list[SubtitleSegment],
    output_path: Path,
    style_key: str = DEFAULT_STYLE,
    font_key: str = DEFAULT_FONT,
    position_key: str = DEFAULT_POSITION,
    color_key: str = DEFAULT_COLOR,
) -> Path:
    ffmpeg_bin = get_ffmpeg_binary()
    work_dir = output_path.parent

    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key
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
        args += ["-af", AUDIO_FILTER, "-c:a", "aac"]

    args += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path.name,
    ]

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
) -> Path:
    """Один кадр с субтитрами — быстрый предпросмотр перед рендером."""
    ffmpeg_bin = get_ffmpeg_binary()
    work_dir = output_path.parent

    filter_expr, mode, _ = _prepare(
        video_path, segments, work_dir, style_key, font_key, position_key, color_key
    )

    if segments:
        first = segments[0]
        ts = min(first.start + 0.4, (first.start + first.end) / 2 + 0.1)
    else:
        ts = 0.5
    ts = max(ts, 0.1)

    # -ss ПОСЛЕ -i (output seeking), чтобы тайминги субтитров совпадали с кадром
    args = [ffmpeg_bin, "-y", "-i", "input.mp4", "-ss", f"{ts:.2f}"]
    if mode == "complex":
        args += ["-filter_complex", filter_expr, "-map", "[v]"]
    else:
        args += ["-vf", filter_expr]
    args += ["-frames:v", "1", "-update", "1", output_path.name]

    _run_ffmpeg(args, work_dir)
    return output_path

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
MAX_SIDE = 1080
ULTRA_MAX_SIDE = 540
ULTRA_TARGET_W = 540
ULTRA_TARGET_H = 960
NORMALIZE_FROM_MB = 30  # двухшаговый рендер: сначала сжать, потом субтитры


def _file_size_mb(path: Path) -> int:
    try:
        return path.stat().st_size // (1024 * 1024)
    except OSError:
        return 0


def _needs_normalize(path: Path) -> bool:
    return _file_size_mb(path) >= NORMALIZE_FROM_MB


def _set_input(work_dir: Path, video_path: Path) -> None:
    local_input = work_dir / "input.mp4"
    if local_input.exists() or local_input.is_symlink():
        local_input.unlink()
    try:
        local_input.symlink_to(video_path.resolve())
    except OSError:
        shutil.copy2(video_path, local_input)


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


def _ffmpeg_error(stderr: str, returncode: int) -> str:
    text = (stderr or "").strip()
    if returncode < 0:
        sig = -returncode
        if sig == 9:
            return (
                "Серверу не хватило памяти при рендере. "
                "Попробуй видео короче (до 2–3 мин) или на Railway увеличь RAM в Settings."
            )
        return f"FFmpeg остановлен сигналом {sig}."
    for line in text.splitlines():
        low = line.lower()
        if any(k in low for k in ("error", "invalid", "failed", "no such", "signal", "killed")):
            return line.strip()[:500]
    tail = text[-800:] if text else "неизвестная ошибка"
    return tail


def _run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка ffmpeg: {_ffmpeg_error(result.stderr, result.returncode)}")


def _normalize_video(source: Path, work_dir: Path, *, ultra: bool = False) -> Path:
    """Шаг 1: сжать тяжёлое видео без субтитров — в разы меньше RAM."""
    out = work_dir / "normalized.mp4"
    if out.exists():
        out.unlink()

    ffmpeg_bin = get_ffmpeg_binary()
    width, height = get_video_size(str(source))
    vertical = _needs_vertical_fit(width, height)

    if ultra:
        tw, th, cap = ULTRA_TARGET_W, ULTRA_TARGET_H, ULTRA_MAX_SIDE
    else:
        tw, th, cap = TARGET_W, TARGET_H, MAX_SIDE

    if vertical:
        vf = (
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        vf = (
            f"scale=w='if(gt(iw,ih),min({cap},iw),-2)'"
            f":h='if(gt(ih,iw),min({cap},ih),-2)'"
        )

    args = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        str(source.resolve()),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "27",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "8192",
        str(out.resolve()),
    ]
    _run_ffmpeg(args)
    return out


def _build_video_filter(
    ass_relpath: str,
    width: int,
    height: int,
    *,
    ultra: bool = False,
) -> tuple[str, str, int, int]:
    """Шаг 2: только субтитры поверх уже сжатого видео."""
    fonts_dir = _escape_filter_path(str(FONTS_DIR))
    ass_filter = f"ass={ass_relpath}:fontsdir={fonts_dir}"
    return ass_filter, "vf", width, height


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
    *,
    ultra: bool = False,
) -> tuple[str, str]:
    ass_path = work_dir / "subs.ass"
    _set_input(work_dir, video_path)

    width, height = get_video_size(str(work_dir / "input.mp4"))
    filter_expr, mode, canvas_w, canvas_h = _build_video_filter(
        "subs.ass", width, height, ultra=ultra
    )

    ass_text = build_ass(
        segments,
        style_key,
        font_key,
        position_key,
        color_key,
        canvas_w,
        canvas_h,
        size_key,
        for_preview=for_preview,
        preview_ts=preview_ts,
    )
    ass_path.write_text(ass_text, encoding="utf-8")
    return filter_expr, mode


def _build_render_command(
    work_dir: Path,
    filter_expr: str,
    output_name: str,
    *,
    ultra: bool = False,
) -> list[str]:
    ffmpeg_bin = get_ffmpeg_binary()
    audio = has_audio_stream(str(work_dir / "input.mp4"))

    args = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        "input.mp4",
        "-vf",
        filter_expr,
        "-map",
        "0:v",
    ]
    if audio:
        args += ["-map", "0:a?", "-c:a", "aac", "-b:a", "96k"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast" if ultra else "veryfast",
        "-crf",
        "25" if ultra else "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "8192",
        output_name,
    ]
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
    heavy = _needs_normalize(video_path)

    if heavy:
        attempts: list[tuple[bool, str]] = [
            (False, "lite"),
            (True, "ultra"),
        ]
    else:
        attempts = [(False, "normal")]

    last_err: Exception | None = None
    for ultra, _label in attempts:
        try:
            source = video_path
            if heavy:
                source = _normalize_video(video_path, work_dir, ultra=ultra)

            filter_expr, mode = _prepare(
                source,
                segments,
                work_dir,
                style_key,
                font_key,
                position_key,
                color_key,
                size_key,
                ultra=ultra,
            )
            args = _build_render_command(
                work_dir, filter_expr, output_path.name, ultra=ultra or heavy
            )
            _run_ffmpeg(args, work_dir)
            return output_path
        except RuntimeError as exc:
            last_err = exc

    raise RuntimeError(str(last_err) if last_err else "Ошибка рендера")


def _preview_filter(
    ass_relpath: str,
    width: int,
    height: int,
    vertical: bool,
) -> str:
    fonts_dir = _escape_filter_path(str(FONTS_DIR))
    ass_filter = f"ass={ass_relpath}:fontsdir={fonts_dir}"
    if vertical:
        return (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
            f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"{ass_filter}"
        )
    cap = MAX_SIDE
    if max(width, height) > cap:
        return (
            f"scale=w='if(gt(iw,ih),min({cap},iw),-2)'"
            f":h='if(gt(ih,iw),min({cap},ih),-2)',"
            f"{ass_filter}"
        )
    return ass_filter


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
    ts = _pick_preview_ts(segments, video_path)

    _set_input(work_dir, video_path)
    width, height = get_video_size(str(work_dir / "input.mp4"))
    vertical = _needs_vertical_fit(width, height)
    canvas_w, canvas_h = (TARGET_W, TARGET_H) if vertical else (width, height)

    ass_path = work_dir / "subs.ass"
    ass_path.write_text(
        build_ass(
            segments,
            style_key,
            font_key,
            position_key,
            color_key,
            canvas_w,
            canvas_h,
            size_key,
            for_preview=True,
            preview_ts=ts,
        ),
        encoding="utf-8",
    )
    vf = _preview_filter("subs.ass", width, height, vertical)

    args = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{ts:.3f}",
        "-i",
        "input.mp4",
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-update",
        "1",
        output_path.name,
    ]
    _run_ffmpeg(args, work_dir)
    return output_path

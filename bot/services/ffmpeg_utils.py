import os
import shutil
import subprocess
from functools import lru_cache


FFMPEG_FULL_PATHS = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
)


@lru_cache(maxsize=1)
def get_ffmpeg_binary() -> str:
    custom = os.getenv("FFMPEG_PATH", "").strip()
    if custom:
        return custom

    for path in FFMPEG_FULL_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    found = shutil.which("ffmpeg")
    if found:
        return found

    raise RuntimeError(
        "FFmpeg не найден. Установи: brew install ffmpeg-full"
    )


@lru_cache(maxsize=1)
def get_ffprobe_binary() -> str:
    ffmpeg_bin = get_ffmpeg_binary()
    candidate = ffmpeg_bin.rsplit("ffmpeg", 1)
    if len(candidate) == 2:
        probe = "ffprobe".join(candidate)
        if os.path.isfile(probe) and os.access(probe, os.X_OK):
            return probe

    found = shutil.which("ffprobe")
    if found:
        return found

    return "ffprobe"


def get_video_size(video_path: str) -> tuple[int, int]:
    ffprobe = get_ffprobe_binary()
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                video_path,
            ],
            capture_output=True,
            text=True,
        )
        width, height = result.stdout.strip().split("x")
        return int(width), int(height)
    except Exception:
        return 1080, 1920


def has_audio_stream(video_path: str) -> bool:
    ffprobe = get_ffprobe_binary()
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True


def _ffmpeg_supports_subtitles(ffmpeg_bin: str) -> bool:
    result = subprocess.run(
        [ffmpeg_bin, "-h", "filter=subtitles"],
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    return "Unknown filter" not in output and "Render text subtitles" in output


def check_ffmpeg() -> str:
    ffmpeg_bin = get_ffmpeg_binary()

    try:
        subprocess.run(
            [ffmpeg_bin, "-version"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "FFmpeg не установлен. Установи: brew install ffmpeg-full"
        ) from exc

    if not _ffmpeg_supports_subtitles(ffmpeg_bin):
        raise RuntimeError(
            "Твой FFmpeg не умеет субтитры. Выполни:\n"
            "brew install ffmpeg-full\n"
            'echo \'export PATH="/opt/homebrew/opt/ffmpeg-full/bin:$PATH"\' >> ~/.zprofile\n'
            "source ~/.zprofile"
        )

    return ffmpeg_bin

import shutil
import subprocess
import tempfile
from pathlib import Path

from bot.services.ffmpeg_utils import check_ffmpeg
from bot.services.render_video import render_preview_image, render_video_with_subtitles
from bot.services.style_text import style_subtitles
from bot.services.transcribe import SubtitleSegment, transcribe_video


class VideoProcessingError(Exception):
    pass


def prepare_segments(client, video_path: Path) -> list[SubtitleSegment]:
    """Транскрибация + стилизация текста."""
    check_ffmpeg()

    work_dir = Path(tempfile.mkdtemp(prefix="reels-bot-prep-"))
    try:
        segments = transcribe_video(client, video_path, work_dir)
        if not segments:
            raise VideoProcessingError("Не удалось распознать речь в видео")
        try:
            segments = style_subtitles(client, segments)
        except Exception:
            pass
        return segments
    except VideoProcessingError:
        raise
    except RuntimeError as exc:
        raise VideoProcessingError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or b"").decode("utf-8", errors="ignore")
        raise VideoProcessingError(details[-1200:] or "Ошибка ffmpeg") from exc
    except Exception as exc:
        raise VideoProcessingError(f"Ошибка обработки: {exc}") from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def render_segments(
    video_path: Path,
    segments: list[SubtitleSegment],
    style_key: str,
    font_key: str,
    position_key: str,
    color_key: str,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "result.mp4"
    try:
        render_video_with_subtitles(
            video_path, segments, output_path, style_key, font_key, position_key, color_key
        )
        return output_path
    except RuntimeError as exc:
        raise VideoProcessingError(str(exc)) from exc
    except Exception as exc:
        raise VideoProcessingError(f"Ошибка рендера: {exc}") from exc


def render_preview(
    video_path: Path,
    segments: list[SubtitleSegment],
    style_key: str,
    font_key: str,
    position_key: str,
    color_key: str,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "preview.png"
    try:
        render_preview_image(
            video_path, segments, output_path, style_key, font_key, position_key, color_key
        )
        return output_path
    except RuntimeError as exc:
        raise VideoProcessingError(str(exc)) from exc
    except Exception as exc:
        raise VideoProcessingError(f"Ошибка превью: {exc}") from exc

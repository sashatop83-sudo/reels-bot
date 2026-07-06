from dataclasses import dataclass, field
from pathlib import Path

from bot.services.ffmpeg_utils import get_ffmpeg_binary

WHISPER_MODEL = "whisper-large-v3-turbo"


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    import subprocess

    ffmpeg_bin = get_ffmpeg_binary()
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "4",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )


def _get(obj, key, default=None):
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _parse_words(response) -> list[Word]:
    raw_words = _get(response, "words") or []
    words: list[Word] = []
    for item in raw_words:
        text = (_get(item, "word") or _get(item, "text") or "").strip()
        if not text:
            continue
        try:
            start = float(_get(item, "start"))
            end = float(_get(item, "end"))
        except (TypeError, ValueError):
            continue
        words.append(Word(text=text, start=start, end=end))
    return words


def _assign_words(segments: list[SubtitleSegment], words: list[Word]) -> None:
    if not words:
        return
    for segment in segments:
        segment.words = [
            w for w in words if w.start >= segment.start - 0.05 and w.start < segment.end + 0.05
        ]


def _parse_segments(response) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    raw_segments = _get(response, "segments") or []

    for item in raw_segments:
        text = (_get(item, "text") or "").strip()
        if not text:
            continue
        try:
            start = float(_get(item, "start"))
            end = float(_get(item, "end"))
        except (TypeError, ValueError):
            continue
        segments.append(SubtitleSegment(start=start, end=end, text=text))

    words = _parse_words(response)
    _assign_words(segments, words)

    if not segments and _get(response, "text"):
        segments.append(
            SubtitleSegment(start=0.0, end=5.0, text=str(_get(response, "text")).strip())
        )

    return segments


def transcribe_video(client, video_path: Path, work_dir: Path) -> list[SubtitleSegment]:
    audio_path = work_dir / "audio.mp3"
    _extract_audio(video_path, audio_path)

    with audio_path.open("rb") as audio_file:
        try:
            response = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment", "word"],
            )
        except Exception:
            audio_file.seek(0)
            response = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
                response_format="json",
            )

    return _parse_segments(response)

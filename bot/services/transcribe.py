from dataclasses import dataclass, field
from pathlib import Path

from bot.services.ffmpeg_utils import get_ffmpeg_binary, get_media_duration

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
            "2",
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
    if not words or not segments:
        return
    for segment in segments:
        segment.words = []
    for word in words:
        mid = (word.start + word.end) / 2
        target = None
        for segment in segments:
            if segment.start - 0.08 <= mid <= segment.end + 0.08:
                target = segment
                break
        if target is None:
            target = min(
                segments,
                key=lambda s: min(abs(mid - s.start), abs(mid - s.end)),
            )
        target.words.append(word)
    for segment in segments:
        segment.words.sort(key=lambda w: w.start)


def _parse_segments(response, total_duration: float = 0.0) -> list[SubtitleSegment]:
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
        full = str(_get(response, "text")).strip()
        end = total_duration if total_duration and total_duration > 1 else max(len(full.split()) * 0.5, 3.0)
        segments.append(SubtitleSegment(start=0.0, end=end, text=full))

    return segments


def transcribe_video(client, video_path: Path, work_dir: Path) -> list[SubtitleSegment]:
    audio_path = work_dir / "audio.mp3"
    _extract_audio(video_path, audio_path)

    response = None
    # 1) лучший вариант — сегменты + слова
    try:
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
                language="ru",
                response_format="verbose_json",
                timestamp_granularities=["segment", "word"],
            )
    except Exception:
        response = None

    # 2) запасной — хотя бы сегменты с таймкодами (НЕ плоский json!)
    if response is None or not (_get(response, "segments")):
        try:
            with audio_path.open("rb") as audio_file:
                response = client.audio.transcriptions.create(
                    model=WHISPER_MODEL,
                    file=audio_file,
                    language="ru",
                    response_format="verbose_json",
                )
        except Exception:
            response = None

    # 3) крайний случай — просто текст
    if response is None:
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
                language="ru",
                response_format="json",
            )

    duration = get_media_duration(str(audio_path))
    return _parse_segments(response, duration)

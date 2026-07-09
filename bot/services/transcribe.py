from dataclasses import dataclass, field
from pathlib import Path

from bot.services.ffmpeg_utils import get_ffmpeg_binary, get_media_duration

# large-v3 — максимальная точность на Groq (turbo только если упадёт)
WHISPER_MODEL = "whisper-large-v3"
WHISPER_FALLBACK = "whisper-large-v3-turbo"

# Подсказка модели — меньше ошибок в русской разговорной речи
RUSSIAN_PROMPT = (
    "Разговорная речь на русском языке. Видео, блог, reels, сторис. "
    "Транскрибируй слова точно как произнесены, без выдумывания."
)


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
    # Чистим голос для Whisper: убираем низкий гул, нормализуем громкость
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-af",
            "highpass=f=80,lowpass=f=9000,loudnorm=I=-16:TP=-1.5:LRA=11",
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


def _smooth_words(words: list[Word]) -> list[Word]:
    """Сгладить тайминги слов — меньше рывков на видео."""
    if not words:
        return []
    words = sorted(words, key=lambda w: w.start)
    smoothed: list[Word] = []

    for i, word in enumerate(words):
        start = word.start
        end = max(word.end, start + 0.07)

        if i + 1 < len(words):
            nxt = words[i + 1]
            gap = nxt.start - end
            if 0 < gap < 0.35:
                end = nxt.start
            elif gap < 0:
                end = min(end, nxt.start - 0.02)

        if smoothed:
            prev = smoothed[-1]
            if start < prev.end:
                start = prev.end
            if start - prev.end > 0.5:
                pass
            elif start - prev.end < 0.02:
                start = prev.end

        if end <= start:
            end = start + 0.07
        smoothed.append(Word(text=word.text, start=start, end=end))

    return smoothed


def _segments_from_words(words: list[Word], pause_gap: float = 0.55) -> list[SubtitleSegment]:
    """Сегменты из потока слов — текст и тайминги из одного источника."""
    if not words:
        return []

    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current and word.start - current[-1].end > pause_gap:
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)

    segments: list[SubtitleSegment] = []
    for group in groups:
        text = " ".join(w.text for w in group).strip()
        if not text:
            continue
        segments.append(
            SubtitleSegment(
                start=group[0].start,
                end=max(group[-1].end, group[0].start + 0.2),
                text=text,
                words=list(group),
            )
        )
    return segments


def _parse_segments_fallback(response, total_duration: float = 0.0) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    for item in _get(response, "segments") or []:
        text = (_get(item, "text") or "").strip()
        if not text:
            continue
        try:
            start = float(_get(item, "start"))
            end = float(_get(item, "end"))
        except (TypeError, ValueError):
            continue
        segments.append(SubtitleSegment(start=start, end=end, text=text))

    if not segments and _get(response, "text"):
        full = str(_get(response, "text")).strip()
        end = total_duration if total_duration > 1 else max(len(full.split()) * 0.5, 3.0)
        segments.append(SubtitleSegment(start=0.0, end=end, text=full))

    return segments


def _transcribe_once(client, audio_path: Path, model: str, with_words: bool):
    params: dict = {
        "model": model,
        "language": "ru",
        "prompt": RUSSIAN_PROMPT,
        "response_format": "verbose_json",
        "temperature": 0,
    }
    if with_words:
        params["timestamp_granularities"] = ["word", "segment"]

    with audio_path.open("rb") as audio_file:
        return client.audio.transcriptions.create(file=audio_file, **params)


def transcribe_video(client, video_path: Path, work_dir: Path) -> list[SubtitleSegment]:
    audio_path = work_dir / "audio.wav"
    _extract_audio(video_path, audio_path)
    duration = get_media_duration(str(audio_path))

    response = None
    for model in (WHISPER_MODEL, WHISPER_FALLBACK):
        try:
            response = _transcribe_once(client, audio_path, model, with_words=True)
            if _parse_words(response) or _get(response, "segments"):
                break
        except Exception:
            response = None

    if response is None:
        for model in (WHISPER_MODEL, WHISPER_FALLBACK):
            try:
                response = _transcribe_once(client, audio_path, model, with_words=False)
                if _get(response, "segments"):
                    break
            except Exception:
                response = None

    if response is None:
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
                language="ru",
                prompt=RUSSIAN_PROMPT,
                response_format="json",
                temperature=0,
            )

    words = _smooth_words(_parse_words(response))
    if words:
        return _segments_from_words(words)

    segments = _parse_segments_fallback(response, duration)
    for seg in segments:
        seg.words = _smooth_words(_synth_words(seg))
    return segments


def _synth_words(segment: SubtitleSegment) -> list[Word]:
    tokens = [t for t in segment.text.split() if t]
    if not tokens:
        return []
    duration = max(segment.end - segment.start, 0.4)
    step = duration / len(tokens)
    return [
        Word(text=tok, start=segment.start + i * step, end=segment.start + (i + 1) * step)
        for i, tok in enumerate(tokens)
    ]

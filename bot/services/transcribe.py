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

    video_duration = get_media_duration(str(video_path))
    target_duration = video_duration if video_duration > 0 else duration

    words = _smooth_words(_parse_words(response))
    if words:
        segments = _segments_from_words(words)
    else:
        segments = _parse_segments_fallback(response, duration)
        for seg in segments:
            seg.words = _smooth_words(_synth_words(seg))

    return align_segments_to_duration(segments, target_duration)


def _synth_words(segment: SubtitleSegment) -> list[Word]:
    tokens = [t for t in segment.text.split() if t]
    if not tokens:
        return []
    times = _map_token_times(tokens, [], segment.start, segment.end)
    return [Word(text=tok, start=s, end=e) for tok, (s, e) in zip(tokens, times)]


def collect_all_words(segments: list[SubtitleSegment]) -> list[Word]:
    words: list[Word] = []
    for segment in segments:
        if segment.words:
            words.extend(segment.words)
        elif segment.text.strip():
            words.extend(_synth_words(segment))
    return sorted(words, key=lambda w: w.start)


def _map_token_times(
    tokens: list[str],
    ref_words: list[Word],
    fallback_start: float,
    fallback_end: float,
) -> list[tuple[float, float]]:
    """Распределить тайминги новых слов по ритму старых (Whisper), а не равными кусками."""
    n = len(tokens)
    if n == 0:
        return []

    if not ref_words:
        duration = max(fallback_end - fallback_start, 0.4)
        weights = [max(len(t), 1) for t in tokens]
        total_w = sum(weights) or 1
        out: list[tuple[float, float]] = []
        t = fallback_start
        for weight in weights:
            d = max(duration * (weight / total_w), 0.07)
            out.append((t, t + d))
            t += d
        if out:
            s, _ = out[-1]
            out[-1] = (s, max(fallback_end, s + 0.07))
        return out

    if n == 1:
        return [(ref_words[0].start, max(ref_words[-1].end, ref_words[0].start + 0.07))]

    n_ref = len(ref_words)
    out: list[tuple[float, float]] = []
    for i in range(n):
        pos = i * (n_ref - 1) / (n - 1)
        idx = int(pos)
        frac = pos - idx
        if idx >= n_ref - 1:
            w = ref_words[-1]
            out.append((w.start, max(w.end, w.start + 0.07)))
        else:
            w0, w1 = ref_words[idx], ref_words[idx + 1]
            start = w0.start + frac * (w1.start - w0.start)
            end = w0.end + frac * (w1.end - w0.end)
            if end <= start:
                end = start + 0.07
            out.append((start, end))

    for i in range(1, len(out)):
        prev_end = out[i - 1][1]
        s, e = out[i]
        if s < prev_end:
            s = prev_end
        if e <= s:
            e = s + 0.07
        out[i] = (s, e)
    return out


def update_segment_words(segment: SubtitleSegment, new_text: str | None = None) -> None:
    """Обновить текст сегмента, сохранив тайминги слов от Whisper."""
    text = (new_text if new_text is not None else segment.text).strip()
    segment.text = text
    tokens = [t for t in text.split() if t]
    if not tokens:
        segment.words = []
        return
    ref = list(segment.words) if segment.words else []
    times = _map_token_times(tokens, ref, segment.start, segment.end)
    segment.words = [Word(text=tok, start=s, end=e) for tok, (s, e) in zip(tokens, times)]
    segment.start = segment.words[0].start
    segment.end = max(segment.words[-1].end, segment.start + 0.2)


def _lines_by_char_weight(lines: list[str], t_start: float, t_end: float) -> list[SubtitleSegment]:
    weights = [max(len(ln), 1) for ln in lines]
    total = sum(weights) or 1
    duration = max(t_end - t_start, 0.5)
    out: list[SubtitleSegment] = []
    t = t_start
    acc = 0
    for i, (line, weight) in enumerate(zip(lines, weights)):
        acc += weight
        seg_end = t_end if i == len(lines) - 1 else t_start + duration * (acc / total)
        seg = SubtitleSegment(start=t, end=seg_end, text=line, words=[])
        update_segment_words(seg, line)
        out.append(seg)
        t = seg_end
    return out


def rebuild_segments_from_lines(
    old_segments: list[SubtitleSegment],
    new_lines: list[str],
) -> list[SubtitleSegment]:
    """Пересобрать сегменты после полной правки текста — тайминги от исходной речи."""
    lines = [ln.strip() for ln in new_lines if ln.strip()]
    if not lines or not old_segments:
        return []

    if len(lines) == len(old_segments):
        out: list[SubtitleSegment] = []
        for old, line in zip(old_segments, lines):
            seg = SubtitleSegment(
                start=old.start,
                end=old.end,
                text=line,
                words=list(old.words),
            )
            update_segment_words(seg, line)
            out.append(seg)
        return out

    old_words = collect_all_words(old_segments)
    t_start = old_segments[0].start
    t_end = old_segments[-1].end
    line_tokens = [[t for t in ln.split() if t] for ln in lines]
    all_tokens = [tok for group in line_tokens for tok in group]

    if not all_tokens:
        return _lines_by_char_weight(lines, t_start, t_end)

    times = _map_token_times(all_tokens, old_words, t_start, t_end)
    out: list[SubtitleSegment] = []
    idx = 0
    for line, tokens in zip(lines, line_tokens):
        if not tokens:
            continue
        words = [
            Word(text=tokens[j], start=times[idx + j][0], end=times[idx + j][1])
            for j in range(len(tokens))
        ]
        idx += len(tokens)
        out.append(
            SubtitleSegment(
                start=words[0].start,
                end=max(words[-1].end, words[0].start + 0.2),
                text=line,
                words=words,
            )
        )
    return out


def _scale_segments(segments: list[SubtitleSegment], ratio: float) -> list[SubtitleSegment]:
    if abs(ratio - 1.0) < 0.001:
        return segments
    out: list[SubtitleSegment] = []
    for seg in segments:
        start = seg.start * ratio
        end = seg.end * ratio
        words = [
            Word(text=w.text, start=w.start * ratio, end=w.end * ratio)
            for w in (seg.words or [])
        ]
        new_seg = SubtitleSegment(start=start, end=end, text=seg.text, words=words)
        if not words and seg.text.strip():
            new_seg.words = _synth_words(new_seg)
        out.append(new_seg)
    return out


def align_segments_to_duration(
    segments: list[SubtitleSegment],
    duration: float,
    *,
    tolerance: float = 0.04,
) -> list[SubtitleSegment]:
    """Подогнать тайминги, если речь длиннее видео (ускоренный ролик, рассинхрон)."""
    if not segments or duration <= 0:
        return segments
    last_end = max(seg.end for seg in segments)
    if last_end <= 0:
        return segments
    if last_end <= duration * (1 + tolerance):
        return segments
    ratio = duration / last_end
    return _scale_segments(segments, ratio)

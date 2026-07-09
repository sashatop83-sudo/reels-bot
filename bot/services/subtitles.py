"""Reels-style subtitle rendering via ASS (libass).

Оформление складывается из трёх независимых частей:
- STYLE  — анимация + цвет (панч, караоке, неон, огонь…)
- FONT   — шрифт (Montserrat, Oswald, Caveat…)
- POSITION — где текст (сверху / по центру / снизу)
"""

import re
from dataclasses import dataclass
from pathlib import Path

from bot.services.transcribe import SubtitleSegment, Word

FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"


# ---------- Шрифты ----------

@dataclass(frozen=True)
class FontChoice:
    key: str
    label: str
    family: str
    size_mult: float


FONTS: dict[str, FontChoice] = {
    "montserrat": FontChoice("montserrat", "Инста-чистый", "Montserrat", 1.0),
    "russo": FontChoice("russo", "Жирный блок", "Russo One", 1.0),
    "oswald": FontChoice("oswald", "Высокий тренд", "Oswald", 1.12),
    "unbounded": FontChoice("unbounded", "Модный", "Unbounded", 0.9),
    "rubik": FontChoice("rubik", "Простой", "Rubik", 1.0),
    "caveat": FontChoice("caveat", "От руки ✍️", "Caveat", 1.4),
    "pattaya": FontChoice("pattaya", "Кисть 🖌", "Pattaya", 1.3),
    "yeseva": FontChoice("yeseva", "Элегант 👑", "Yeseva One", 1.1),
}
FONT_ORDER = ["montserrat", "russo", "oswald", "unbounded", "rubik", "caveat", "pattaya", "yeseva"]
DEFAULT_FONT = "montserrat"


def get_font(key: str) -> FontChoice:
    return FONTS.get(key, FONTS[DEFAULT_FONT])


# ---------- Позиция ----------

@dataclass(frozen=True)
class Position:
    key: str
    label: str
    alignment: int      # ASS numpad
    margin_v_ratio: float


POSITIONS: dict[str, Position] = {
    "top": Position("top", "⬆️ Сверху", 8, 0.12),
    "center": Position("center", "↕️ По центру", 5, 0.0),
    "bottom": Position("bottom", "⬇️ Снизу", 2, 0.16),
}
POSITION_ORDER = ["top", "center", "bottom"]
DEFAULT_POSITION = "center"


def get_position(key: str) -> Position:
    return POSITIONS.get(key, POSITIONS[DEFAULT_POSITION])


# ---------- Размер текста ----------

@dataclass(frozen=True)
class SizeChoice:
    key: str
    label: str
    mult: float


SIZES: dict[str, SizeChoice] = {
    "s": SizeChoice("s", "🔸 Мелкий", 0.8),
    "m": SizeChoice("m", "🔹 Средний", 1.0),
    "l": SizeChoice("l", "🔶 Крупный", 1.25),
}
SIZE_ORDER = ["s", "m", "l"]
DEFAULT_SIZE = "m"


def get_size(key: str) -> SizeChoice:
    return SIZES.get(key, SIZES[DEFAULT_SIZE])


# ---------- Стиль (анимация + цвет) ----------

WHITE = "&H00FFFFFF"
YELLOW = "&H0000FFFF"
ORANGE = "&H0000A5FF"
MINT = "&H006EE63C"
PINK = "&H009314FF"
CYAN = "&H00FFE500"
GREEN = "&H006EFF6E"
RED = "&H000000FF"
PURPLE = "&H00FF20A0"
BLUE = "&H00FF6E1E"
BLACK = "&H00000000"


# ---------- Цвета акцента ----------

@dataclass(frozen=True)
class ColorChoice:
    key: str
    label: str
    value: str | None   # None = использовать цвет стиля


COLORS: dict[str, ColorChoice] = {
    "default": ColorChoice("default", "🎨 Как в стиле", None),
    "white": ColorChoice("white", "⬜ Белый", WHITE),
    "yellow": ColorChoice("yellow", "💛 Жёлтый", YELLOW),
    "orange": ColorChoice("orange", "🧡 Оранж", ORANGE),
    "red": ColorChoice("red", "❤️ Красный", RED),
    "pink": ColorChoice("pink", "💗 Розовый", PINK),
    "mint": ColorChoice("mint", "🌿 Мята", MINT),
    "green": ColorChoice("green", "💚 Зелёный", GREEN),
    "cyan": ColorChoice("cyan", "🩵 Циан", CYAN),
    "purple": ColorChoice("purple", "💜 Фиолет", PURPLE),
    "blue": ColorChoice("blue", "💙 Синий", BLUE),
}
COLOR_ORDER = ["default", "white", "yellow", "orange", "red", "pink", "mint", "green", "cyan", "purple", "blue"]
DEFAULT_COLOR = "default"


def get_color(key: str) -> ColorChoice:
    return COLORS.get(key, COLORS[DEFAULT_COLOR])


@dataclass(frozen=True)
class SubtitleStyle:
    key: str
    label: str
    mode: str            # "punch" | "karaoke" | "line"
    primary: str         # цвет текста
    highlight: str       # цвет активного слова (karaoke)
    outline_colour: str
    outline: int
    shadow: int
    fontsize_ratio: float
    uppercase: bool
    blur: int = 0
    desc: str = ""


STYLES: dict[str, SubtitleStyle] = {
    # ⚡️ ХАЙЛАЙТ — самый трендовый: слова появляются по одному, активное вспыхивает жёлтым
    "hormozi": SubtitleStyle("hormozi", "⚡️ Хайлайт", "karaoke", WHITE, YELLOW, BLACK, 7, 2, 0.062, True,
                             desc="Слово за словом, активное — жёлтым. Тренд TikTok/Reels."),
    # 🔥 ПАНЧ — крупные слова по 2, выпрыгивают
    "punch": SubtitleStyle("punch", "🔥 Панч", "punch", WHITE, YELLOW, BLACK, 6, 2, 0.070, True,
                           desc="Крупные слова по 2, эффект выпрыгивания."),
    # 🌶 ОГОНЬ — жирный оранжевый
    "fire": SubtitleStyle("fire", "🌶 Огонь", "punch", ORANGE, ORANGE, BLACK, 6, 2, 0.070, True,
                          desc="Оранжевый жирный панч, агрессивно и ярко."),
    # 🎤 КАРАОКЕ — плавная заливка слов по ходу речи
    "karaoke": SubtitleStyle("karaoke", "🎤 Караоке", "karaoke", WHITE, CYAN, BLACK, 5, 1, 0.056, False,
                             desc="Слова заливаются цветом по ходу речи."),
    # 🌿 МЯТА — мягкий мятный акцент
    "mint": SubtitleStyle("mint", "🌿 Мята", "karaoke", WHITE, MINT, BLACK, 5, 1, 0.056, False,
                          desc="Спокойный мятный акцент, чисто и мягко."),
    # 💗 РОЗОВЫЙ — гламурный
    "pink": SubtitleStyle("pink", "💗 Розовый", "karaoke", WHITE, PINK, BLACK, 5, 1, 0.056, False,
                          desc="Розовый акцент, для лайфстайл/бьюти."),
    # 💜 НЕОН — свечение
    "neon": SubtitleStyle("neon", "💜 Неон", "line", WHITE, WHITE, PURPLE, 3, 0, 0.058, True, blur=4,
                          desc="Неоновое свечение, вайб ночного TikTok."),
    # ⚪️ МИНИМАЛ — чистый белый, как подписи в Instagram
    "minimal": SubtitleStyle("minimal", "⚪️ Минимал", "line", WHITE, YELLOW, BLACK, 3, 1, 0.050, False,
                             desc="Чистый белый текст, минимализм Instagram."),
    # 🎬 КЛАССИКА — как субтитры в кино
    "classic": SubtitleStyle("classic", "🎬 Классика", "line", WHITE, WHITE, BLACK, 4, 1, 0.048, False,
                             desc="Обычные аккуратные субтитры, как в кино."),
}
STYLE_ORDER = ["hormozi", "punch", "fire", "karaoke", "mint", "pink", "neon", "minimal", "classic"]
DEFAULT_STYLE = "hormozi"

PUNCH_CHUNK_SIZE = 2
KARAOKE_MAX_WORDS = 4
KARAOKE_MAX_CHARS = 26
LINE_MAX_CHARS = 28
LINE_MAX_DUR = 2.8
FADE_MS = 120
WORD_MIN_CS = 4


def get_style(key: str) -> SubtitleStyle:
    return STYLES.get(key, STYLES[DEFAULT_STYLE])


# ---------- ASS builder ----------

def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        centis = 99
    return f"{hours:01d}:{minutes:02d}:{secs:02d}.{centis:02d}"


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D\U00002190-\U000021FF]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    # шрифты не содержат эмодзи-глифов → убираем, чтобы не было квадратов
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip()


def _escape(text: str) -> str:
    text = _strip_emoji(text)
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .strip()
    )


def _escape_word(text: str) -> str:
    # для панч/караоке: убираем служебные звёздочки
    return _escape(text).replace("*", "")


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


def _all_words(segments: list[SubtitleSegment]) -> list[Word]:
    words: list[Word] = []
    for segment in segments:
        words.extend(segment.words or _synth_words(segment))
    return words


def _group_words(words: list[Word], max_words: int, max_chars: int, max_dur: float) -> list[list[Word]]:
    """Разбить поток слов на короткие строки (по числу слов / символов / длительности)."""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    cur_chars = 0
    for w in words:
        wlen = len(w.text)
        too_many = len(cur) >= max_words
        too_long = cur and cur_chars + wlen + 1 > max_chars
        too_far = cur and (w.end - cur[0].start) > max_dur
        if cur and (too_many or too_long or too_far):
            lines.append(cur)
            cur, cur_chars = [], 0
        cur.append(w)
        cur_chars += wlen + 1
    if cur:
        lines.append(cur)
    return lines


def _split_text_timed(seg: SubtitleSegment, max_chars: int) -> list[tuple[float, float, str]]:
    """Разбить текст сегмента на короткие строки, распределив тайминг пропорционально длине."""
    tokens = seg.text.split()
    if not tokens:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for tok in tokens:
        if cur and cur_len + len(tok) + 1 > max_chars:
            chunks.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(tok)
        cur_len += len(tok) + 1
    if cur:
        chunks.append(" ".join(cur))

    total_chars = sum(len(c) for c in chunks) or 1
    duration = max(seg.end - seg.start, 0.4)
    out: list[tuple[float, float, str]] = []
    t = seg.start
    for c in chunks:
        d = max(duration * (len(c) / total_chars), 0.3)
        out.append((t, t + d, c))
        t += d
    if out:
        last_start, _, last_text = out[-1]
        out[-1] = (last_start, max(seg.end, last_start + 0.3), last_text)
    return out


def _header(
    style: SubtitleStyle,
    font: FontChoice,
    position: Position,
    width: int,
    height: int,
    size_mult: float = 1.0,
    outline_override: int | None = None,
) -> str:
    fontsize = max(20, int(height * style.fontsize_ratio * font.size_mult * size_mult))
    margin_v = int(height * position.margin_v_ratio)
    margin_h = int(width * 0.07)
    outline = outline_override if outline_override is not None else style.outline
    shadow = max(style.shadow, 3) if outline_override else style.shadow
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: R,{font.family},{fontsize},{style.primary},{WHITE},{style.outline_colour},&H64000000,1,0,0,0,100,100,0,0,1,{outline},{shadow},{position.alignment},{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _prefix(style: SubtitleStyle) -> str:
    return f"{{\\blur{style.blur}}}" if style.blur else ""


def _dialogue(start: float, end: float, text: str, fade_ms: int = FADE_MS) -> str:
    fade = max(0, fade_ms)
    if fade:
        text = f"{{\\fad({fade},{fade})}}{text}"
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},R,,0,0,0,,{text}\n"


def _highlight_keywords(escaped_text: str, accent: str, base: str) -> str:
    """Слова, помеченные *звёздочками*, красим в акцентный цвет."""
    def repl(m: re.Match) -> str:
        return f"{{\\c{accent}}}{m.group(1)}{{\\c{base}}}"

    result = re.sub(r"\*(.+?)\*", repl, escaped_text)
    return result.replace("*", "")  # убрать одиночные звёздочки (если пара разорвалась)


def _line_timing(start: float, end: float, pad_start: float = 0.04, pad_end: float = 0.12) -> tuple[float, float]:
    return max(0.0, start - pad_start), end + pad_end


def _pop_tag() -> str:
    return "{\\fscx88\\fscy88\\t(0,160,\\fscx100\\fscy100)}"


def _build_punch(style: SubtitleStyle, segments: list[SubtitleSegment], accent: str | None) -> list[str]:
    words = _all_words(segments)
    lines: list[str] = []
    prefix = _prefix(style)
    text_color = accent or style.primary
    for i in range(0, len(words), PUNCH_CHUNK_SIZE):
        chunk = words[i : i + PUNCH_CHUNK_SIZE]
        if not chunk:
            continue
        start, end = _line_timing(chunk[0].start, chunk[-1].end)
        end = max(end, start + 0.35)
        text = _escape_word(" ".join(w.text for w in chunk))
        if style.uppercase:
            text = text.upper()
        body = f"{prefix}{{\\c{text_color}}}{_pop_tag()}{text}"
        lines.append(_dialogue(start, end, body))
    return lines


def _build_karaoke(style: SubtitleStyle, segments: list[SubtitleSegment], accent: str | None) -> list[str]:
    lines: list[str] = []
    prefix = _prefix(style)
    highlight = accent or style.highlight
    words = _all_words(segments)
    for group in _group_words(words, KARAOKE_MAX_WORDS, KARAOKE_MAX_CHARS, LINE_MAX_DUR):
        if not group:
            continue
        start, end = _line_timing(group[0].start, group[-1].end)
        end = max(end, start + 0.4)
        parts: list[str] = []
        prev_end = group[0].start
        for w in group:
            gap_cs = int(round(max(0.0, w.start - prev_end) * 100))
            if gap_cs > 0:
                parts.append(f"{{\\k{min(gap_cs, 30)}}}")
            dur_cs = max(WORD_MIN_CS, int(round((w.end - w.start) * 100)))
            token = _escape_word(w.text)
            if style.uppercase:
                token = token.upper()
            parts.append(f"{{\\kf{dur_cs}\\c{highlight}}}{token} ")
            prev_end = w.end
        body = f"{prefix}{{\\c{WHITE}}}" + "".join(parts)
        lines.append(_dialogue(start, end, body))
    return lines


def _build_line(style: SubtitleStyle, segments: list[SubtitleSegment], accent: str | None) -> list[str]:
    lines: list[str] = []
    prefix = _prefix(style)
    base = accent or style.primary
    key_accent = accent or style.highlight
    for segment in segments:
        for start, end, chunk_text in _split_text_timed(segment, LINE_MAX_CHARS):
            text = _escape(chunk_text)
            if not text:
                continue
            if style.uppercase:
                text = text.upper()
            text = _highlight_keywords(text, key_accent, base)
            body = f"{prefix}{{\\c{base}}}{text}"
            lines.append(_dialogue(start, max(end, start + 0.45), body))
    return lines


def _preview_text_at_time(style: SubtitleStyle, segments: list[SubtitleSegment], ts: float) -> str:
    """Одна короткая строка для превью — как в видео (2–4 слова), не весь текст."""
    words = _all_words(segments)

    if style.mode == "punch" and words:
        for i, word in enumerate(words):
            if word.start - 0.15 <= ts <= word.end + 0.35:
                chunk = words[i : i + PUNCH_CHUNK_SIZE]
                return " ".join(w.text for w in chunk)
        nearest = min(words, key=lambda w: abs((w.start + w.end) / 2 - ts))
        idx = words.index(nearest)
        chunk = words[idx : idx + PUNCH_CHUNK_SIZE]
        return " ".join(w.text for w in chunk)

    if style.mode == "karaoke" and words:
        groups = _group_words(words, KARAOKE_MAX_WORDS, KARAOKE_MAX_CHARS, LINE_MAX_DUR)
        for group in groups:
            if group[0].start - 0.15 <= ts <= group[-1].end + 0.35:
                return " ".join(w.text for w in group)
        if groups:
            nearest = min(
                groups,
                key=lambda g: abs((g[0].start + g[-1].end) / 2 - ts),
            )
            return " ".join(w.text for w in nearest)

    for segment in segments:
        for start, end, chunk_text in _split_text_timed(segment, LINE_MAX_CHARS):
            if start - 0.15 <= ts <= end + 0.35 and chunk_text.strip():
                return chunk_text

    if words:
        for group in _group_words(words, KARAOKE_MAX_WORDS, KARAOKE_MAX_CHARS, LINE_MAX_DUR):
            if group[0].start - 0.15 <= ts <= group[-1].end + 0.35:
                return " ".join(w.text for w in group)
        return " ".join(w.text for w in words[:KARAOKE_MAX_WORDS])

    for segment in segments:
        if segment.text.strip():
            tokens = segment.text.split()
            return " ".join(tokens[:KARAOKE_MAX_WORDS])
    return ""


def _build_preview(
    style: SubtitleStyle,
    segments: list[SubtitleSegment],
    accent: str | None,
    preview_ts: float,
) -> list[str]:
    """Один яркий кадр для превью — короткая строка, хорошо читается."""
    highlight = accent or style.highlight or style.primary
    raw = _preview_text_at_time(style, segments, preview_ts)
    text = _escape(raw)
    if not text:
        return []
    if style.uppercase:
        text = text.upper()
    body = (
        f"{{\\bord8\\shad4\\3c{BLACK}\\4c&HA0000000}}"
        f"{{\\c{WHITE}}}{{\\1c{highlight}}}{text}"
    )
    start = max(0.0, preview_ts - 2.0)
    end = preview_ts + 8.0
    return [_dialogue(start, end, body)]


def build_ass(
    segments: list[SubtitleSegment],
    style_key: str,
    font_key: str,
    position_key: str,
    color_key: str,
    width: int,
    height: int,
    size_key: str = DEFAULT_SIZE,
    for_preview: bool = False,
    preview_ts: float = 0.0,
) -> str:
    style = get_style(style_key)
    font = get_font(font_key)
    position = get_position(position_key)
    accent = get_color(color_key).value
    size_mult = get_size(size_key).mult
    if for_preview:
        size_mult *= 1.15
        header = _header(style, font, position, width, height, size_mult, outline_override=8)
        events = _build_preview(style, segments, accent, preview_ts)
        return header + "".join(events)

    header = _header(style, font, position, width, height, size_mult)
    if style.mode == "punch":
        events = _build_punch(style, segments, accent)
    elif style.mode == "karaoke":
        events = _build_karaoke(style, segments, accent)
    else:
        events = _build_line(style, segments, accent)

    if not events:
        events = _build_line(style, segments, accent)

    return header + "".join(events)

import json

from openai import OpenAI

from bot.services.transcribe import SubtitleSegment

LLM_MODEL = "llama-3.3-70b-versatile"

STYLE_PROMPT = """Ты редактор субтитров для Reels/TikTok. Работаешь на любом языке (сохраняй язык оригинала).

Перепиши субтитры так, чтобы они выглядели стильно и цепляюще:
- короткие фразы, легко читаются на экране
- 1-2 самых важных слова в фразе оберни звёздочками, вот так: *слово* (их подсветят цветом)
- НЕ используй звёздочки больше 2 раз на фразу
- НЕ добавляй эмодзи
- сохраняй смысл оригинальной речи и язык
- НЕ меняй количество сегментов
- НЕ меняй start/end тайминги

Верни ТОЛЬКО JSON-объект:
{"segments": [{"start": 0.0, "end": 1.5, "text": "..."}]}
"""


def style_subtitles(client: OpenAI, segments: list[SubtitleSegment]) -> list[SubtitleSegment]:
    payload = [{"start": seg.start, "end": seg.end, "text": seg.text} for seg in segments]

    response = client.chat.completions.create(
        model=LLM_MODEL,
        temperature=0.7,
        messages=[
            {"role": "system", "content": STYLE_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    items = data.get("segments") or data.get("subtitles") or [] if isinstance(data, dict) else data
    if not items:
        return segments

    styled: list[SubtitleSegment] = []
    for original, item in zip(segments, items, strict=False):
        text = str(item.get("text", original.text)).strip() or original.text
        styled.append(
            SubtitleSegment(
                start=original.start,
                end=original.end,
                text=text,
                words=original.words,
            )
        )

    if len(styled) < len(segments):
        styled.extend(segments[len(styled):])

    return styled

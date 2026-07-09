import asyncio
import html
import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from bot.config import Settings, create_groq_client
from bot.db import (
    add_bonus,
    can_process_video,
    get_prefs,
    get_recent_users,
    get_stats,
    increment_usage,
    log_event,
    remaining_videos,
    save_prefs,
    set_premium,
    try_set_referrer,
)
from bot.telegram_files import download_telegram_file
from bot.services.payments import invoice_prices, yookassa_provider_data
from bot.services.pipeline import (
    VideoProcessingError,
    prepare_segments,
    render_preview,
    render_segments,
)
from bot.services.subtitles import (
    COLOR_ORDER,
    COLORS,
    DEFAULT_COLOR,
    DEFAULT_FONT,
    DEFAULT_POSITION,
    DEFAULT_SIZE,
    DEFAULT_STYLE,
    FONT_ORDER,
    FONTS,
    POSITION_ORDER,
    POSITIONS,
    SIZE_ORDER,
    SIZES,
    STYLE_ORDER,
    STYLES,
    get_color,
    get_font,
    get_position,
    get_size,
    get_style,
)
from bot.services.transcribe import SubtitleSegment

logger = logging.getLogger(__name__)

MAX_PREVIEW_CHARS = 3000
PAIR_RE = re.compile(r"^\s*(.+?)\s*=\s*(.+?)\s*$")


@dataclass
class Session:
    video_path: Path
    work_dir: Path
    segments: list[SubtitleSegment]
    style: str = DEFAULT_STYLE
    font: str = DEFAULT_FONT
    position: str = DEFAULT_POSITION
    color: str = DEFAULT_COLOR
    size: str = DEFAULT_SIZE
    awaiting_edit: bool = False
    awaiting_full_edit: bool = False
    counted: bool = False
    control_message_id: int | None = None


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.ai_client = create_groq_client(settings.groq_api_key)
        self.offset = 0
        self.sessions: dict[int, Session] = {}
        self.busy: set[int] = set()
        self.render_sem = asyncio.Semaphore(settings.max_concurrent_renders)
        self.bot_username = ""
        self._seen_updates: set[int] = set()

    # ---------- Telegram API ----------

    async def _api(self, method: str, **params):
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{self.base_url}/{method}", json=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram API error"))
            return data["result"]

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None
    ):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        if parse_mode:
            params["parse_mode"] = parse_mode
        return await self._api("sendMessage", **params)

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            return await self._api("editMessageText", **params)
        except Exception:
            return None

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self._api("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:
            pass

    async def delete_message(self, chat_id, message_id) -> None:
        try:
            await self._api("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    async def _send_file(self, method: str, field: str, chat_id, path: Path, caption="", reply_markup=None, mime="video/mp4"):
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        async with httpx.AsyncClient(timeout=600.0) as client:
            with path.open("rb") as fh:
                resp = await client.post(
                    f"{self.base_url}/{method}",
                    data=data,
                    files={field: (path.name, fh, mime)},
                )
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "Telegram API error"))

    async def send_video(self, chat_id, path, caption="", reply_markup=None):
        # как документ — Telegram не пережимает и не ломает пропорции
        await self._send_file(
            "sendDocument", "document", chat_id, path, caption, reply_markup, "video/mp4"
        )

    async def send_photo(self, chat_id, path, caption="", reply_markup=None):
        await self._send_file("sendPhoto", "photo", chat_id, path, caption, reply_markup, "image/png")

    async def download_file(self, file_id: str, destination: Path) -> None:
        await download_telegram_file(self.settings, file_id, destination)

    async def set_commands(self) -> None:
        try:
            await self._api(
                "setMyCommands",
                commands=[
                    {"command": "start", "description": "Начать / загрузить видео"},
                    {"command": "status", "description": "Сколько видео осталось"},
                    {"command": "buy", "description": "💎 PRO-подписка (безлимит)"},
                    {"command": "invite", "description": "👥 Пригласить друга (+видео)"},
                    {"command": "help", "description": "Как пользоваться"},
                ],
            )
        except Exception:
            pass
        # Синяя кнопка "Меню" у поля ввода — открывает список команд
        try:
            await self._api("setChatMenuButton", menu_button={"type": "commands"})
        except Exception:
            pass

    # ---------- Keyboards ----------

    def _main_keyboard(self, session: Session) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": f"🎨 Стиль: {get_style(session.style).label}", "callback_data": "menu:style"},
                ],
                [
                    {"text": f"🔤 Шрифт: {get_font(session.font).label}", "callback_data": "menu:font"},
                    {"text": "🌈 Цвет", "callback_data": "menu:color"},
                ],
                [
                    {"text": f"📍 {get_position(session.position).label}", "callback_data": "menu:pos"},
                    {"text": f"🔠 {get_size(session.size).label}", "callback_data": "menu:size"},
                ],
                [
                    {"text": "✏️ Править текст", "callback_data": "edit:copy"},
                ],
                [
                    {"text": "👁 Превью", "callback_data": "preview"},
                    {"text": "🎬 Сделать видео", "callback_data": "render"},
                ],
            ]
        }

    def _submenu(self, order, registry, current, cb_prefix, per_row=2) -> dict:
        rows, row = [], []
        for key in order:
            mark = "✅ " if key == current else ""
            row.append({"text": f"{mark}{registry[key].label}", "callback_data": f"{cb_prefix}:{key}"})
            if len(row) == per_row:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "⬅️ Назад", "callback_data": "menu:main"}])
        return {"inline_keyboard": rows}

    def _after_render_keyboard(self, session: Session) -> dict:
        kb = self._main_keyboard(session)
        kb["inline_keyboard"].append([{"text": "✔️ Завершить", "callback_data": "finish"}])
        return kb

    def _buy_keyboard(self) -> dict:
        label = (
            f"💳 PRO — {self.settings.price_rub}₽ / {self.settings.sub_days} дн."
            if self.settings.has_yookassa
            else f"💎 PRO — {self.settings.price_rub}₽ / {self.settings.sub_days} дней"
        )
        return {
            "inline_keyboard": [
                [{"text": label, "callback_data": "buy"}],
                [{"text": "👥 Пригласить друга (+видео)", "callback_data": "invite"}],
            ]
        }

    # ---------- Text ----------

    def _clean(self, text: str) -> str:
        return text.replace("*", "")

    def _editable_text(self, segments: list[SubtitleSegment]) -> str:
        return "\n".join(self._clean(seg.text) for seg in segments if seg.text.strip())

    def _format_preview(self, segments: list[SubtitleSegment]) -> str:
        lines = [f"{i}. {self._clean(seg.text)}" for i, seg in enumerate(segments, 1)]
        text = "\n".join(lines)
        if len(text) > MAX_PREVIEW_CHARS:
            text = text[:MAX_PREVIEW_CHARS] + "\n…"
        return text

    def _main_text(self, session: Session) -> str:
        return (
            "🎬 Настройка субтитров\n\n"
            "Текст:\n"
            f"{self._format_preview(session.segments)}\n\n"
            f"🎨 Стиль: {get_style(session.style).label}\n"
            f"🔤 Шрифт: {get_font(session.font).label}\n"
            f"🌈 Цвет: {get_color(session.color).label}\n"
            f"📍 Позиция: {get_position(session.position).label}\n"
            f"🔠 Размер: {get_size(session.size).label}\n\n"
            "✏️ Править текст → кнопка «Править текст» (скопируй, исправь, отправь)\n"
            "👁 Превью — кадр, 🎬 — готовое видео."
        )

    async def _send_control_panel(self, chat_id, session: Session, keyboard: dict | None = None) -> None:
        """Панель настроек ВСЕГДА живёт на текстовом сообщении — тогда кнопки редактируются и работают."""
        if session.control_message_id:
            await self.delete_message(chat_id, session.control_message_id)
            session.control_message_id = None
        control = await self.send_message(
            chat_id, self._main_text(session), reply_markup=keyboard or self._main_keyboard(session)
        )
        session.control_message_id = control["message_id"]

    def welcome_text(self) -> str:
        tg_lim = self.settings.max_video_size_mb
        url_lim = self.settings.max_url_download_mb
        return (
            "👋 Привет! Я делаю видео с крутыми Reels-субтитрами.\n\n"
            "📤 Как отправить видео:\n"
            f"• Файлом в чат — до {tg_lim} MB (лучше как документ 📎)\n"
            f"• Ссылкой — до {url_lim} MB (Google Drive, Яндекс.Диск, Dropbox)\n\n"
            "⚙️ Что дальше:\n"
            "1️⃣ Распознаю речь\n"
            "2️⃣ Выбираешь стиль, шрифт, цвет\n"
            "3️⃣ Поправляешь слова при желании\n"
            "4️⃣ Получаешь готовое видео 🔥\n\n"
            "✨ Подсветка слов, авто-вертикаль, чистый звук.\n"
            f"🎁 Бесплатно: {self.settings.free_video_limit} видео.\n"
            f"💎 PRO — {self.settings.price_rub}₽/мес безлимит. Или зови друзей!\n\n"
            "Пришли видео или ссылку 👇"
        )

    # ---------- Sessions ----------

    def _cleanup_session(self, user_id: int) -> None:
        session = self.sessions.pop(user_id, None)
        if session:
            shutil.rmtree(session.work_dir, ignore_errors=True)

    def _replace_word(self, session: Session, find: str, repl: str) -> int:
        pattern = re.compile(rf"(?<!\w){re.escape(find)}(?!\w)", re.IGNORECASE | re.UNICODE)
        total = 0
        for seg in session.segments:
            new_text, n = pattern.subn(repl, seg.text)
            if n:
                seg.text, seg.words = new_text, []
                total += n
        if total == 0:
            for seg in session.segments:
                if find.lower() in seg.text.lower():
                    seg.text = re.sub(re.escape(find), repl, seg.text, flags=re.IGNORECASE)
                    seg.words = []
                    total += 1
        return total

    def _apply_edits(self, session: Session, text: str) -> str:
        line_edits: dict[int, str] = {}
        word_edits: list[tuple[str, str]] = []
        plain_lines: list[str] = []
        has_eq = False
        for raw in text.splitlines():
            s = raw.strip()
            if not s:
                continue
            m = PAIR_RE.match(s)
            if m:
                has_eq = True
                left, right = m.group(1).strip(), m.group(2).strip()
                if left.isdigit():
                    line_edits[int(left)] = right
                else:
                    word_edits.append((left, right))
            else:
                plain_lines.append(s)

        if has_eq:
            done_pairs: list[str] = []
            replaced = 0
            for f, r in word_edits:
                n = self._replace_word(session, f, r)
                replaced += n
                if n:
                    done_pairs.append(f"{f}→{r}")
            changed = 0
            for idx, new_text in line_edits.items():
                if 1 <= idx <= len(session.segments):
                    session.segments[idx - 1].text = new_text
                    session.segments[idx - 1].words = []
                    changed += 1
            if not replaced and not changed:
                return "Не нашёл что заменить. Проверь слово и пришли ещё раз."
            parts = ["✅ Готово!"]
            if done_pairs:
                parts.append("Заменил: " + ", ".join(done_pairs))
            if changed:
                parts.append(f"Изменил строк: {changed}")
            parts.append("Правки бесплатны. Жми 👁 Превью или 🎬 Сделать видео.")
            return "\n".join(parts)

        cleaned = [re.sub(r"^\s*\d+\.\s*", "", ln) for ln in plain_lines]
        if not cleaned:
            return "Не понял правки. Пришли «слово = правильное» или весь текст списком."
        count = min(len(cleaned), len(session.segments))
        for i in range(count):
            session.segments[i].text = cleaned[i]
            session.segments[i].words = []
        return f"Текст обновлён ({count} строк)."

    def _apply_full_edit(self, session: Session, text: str) -> str:
        lines = [re.sub(r"^\s*\d+[\.\)]\s*", "", ln.strip()) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        if not lines:
            return "Пусто. Скопируй текст из сообщения выше, исправь и отправь снова."

        old = session.segments
        if not old:
            return "Нет текста для правки. Пришли видео заново."

        total_start = old[0].start
        total_end = old[-1].end
        duration = max(total_end - total_start, 0.5)
        step = duration / len(lines)

        new_segments: list[SubtitleSegment] = []
        for i, line in enumerate(lines):
            start = total_start + i * step
            end = total_start + (i + 1) * step if i < len(lines) - 1 else total_end
            new_segments.append(SubtitleSegment(start=start, end=end, text=line, words=[]))

        session.segments = new_segments
        return (
            f"✅ Текст обновлён ({len(lines)} строк).\n"
            "Правки бесплатны. Жми 👁 Превью или 🎬 Сделать видео."
        )

    async def _send_editable_text(self, chat_id: int, session: Session) -> None:
        body = self._editable_text(session.segments)
        if not body:
            await self.send_message(chat_id, "Текст пустой — нечего править.")
            return
        session.awaiting_full_edit = True
        session.awaiting_edit = False
        safe = html.escape(body)
        await self.send_message(
            chat_id,
            "📋 <b>Текст для правки</b> (нажми на блок → Скопировать):\n\n"
            f"<pre>{safe}</pre>\n\n"
            "1. Скопируй весь текст из блока\n"
            "2. Исправь слова где нужно\n"
            "3. Отправь мне одним сообщением\n\n"
            "✅ Правки бесплатны, лимит не тратится.",
            parse_mode="HTML",
        )

    # ---------- Dispatch ----------

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        if "pre_checkout_query" in update:
            await self._handle_pre_checkout(update["pre_checkout_query"])
            return

        message = update.get("message")
        if not message:
            return

        if "successful_payment" in message:
            await self._handle_payment(message)
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = (message.get("text") or "").strip()

        if text.startswith("/start"):
            await self._handle_start(chat_id, user_id, text)
            return
        if text.startswith("/help"):
            await self.send_message(chat_id, self.welcome_text())
            return
        if text.startswith("/status"):
            await self.send_message(chat_id, self._status_text(user_id))
            return
        if text.startswith("/buy"):
            await self._send_invoice(chat_id, user_id)
            return
        if text.startswith("/invite"):
            await self._send_invite(chat_id, user_id)
            return
        if text.startswith("/stats"):
            await self._handle_stats(chat_id, user_id)
            return
        if text.startswith("/users"):
            await self._handle_users(chat_id, user_id)
            return
        if text.startswith("/grant"):
            await self._handle_grant(chat_id, user_id, text)
            return

        if message.get("video") or message.get("document"):
            await self._handle_video(chat_id, user_id, message.get("video"), message.get("document"))
            return

        if text and not text.startswith("/"):
            url = extract_url(text)
            if url:
                await self._handle_url(chat_id, user_id, url)
                return
            session = self.sessions.get(user_id)
            first_line = text.splitlines()[0] if text.splitlines() else ""
            if session and session.awaiting_full_edit:
                result = self._apply_full_edit(session, text)
                session.awaiting_full_edit = False
                await self.send_message(chat_id, result)
                await self._send_control_panel(chat_id, session)
                return
            if session and (session.awaiting_edit or PAIR_RE.match(first_line)):
                result = self._apply_edits(session, text)
                session.awaiting_edit = False
                await self.send_message(chat_id, result)
                await self._send_control_panel(chat_id, session)
                return

        await self.send_message(
            chat_id,
            "Пришли видео или ссылку на него 🎬\n"
            "(Google Drive, Яндекс.Диск, Dropbox или прямая ссылка на mp4)",
        )

    # ---------- Commands ----------

    def _status_text(self, user_id: int) -> str:
        left = remaining_videos(user_id, self.settings.free_video_limit)
        if left == "безлимит":
            return "💎 У тебя активна PRO — безлимит видео!"
        return (
            f"📊 Осталось бесплатных видео: {left}\n\n"
            f"💎 PRO — {self.settings.price_rub}₽/мес, безлимит: /buy\n"
            "👥 Или пригласи друга (+видео обоим): /invite"
        )

    async def _handle_start(self, chat_id: int, user_id: int, text: str) -> None:
        log_event(user_id, "start")
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            ref = int(parts[1].strip())
            if try_set_referrer(user_id, ref, self.settings.referral_bonus):
                await self.send_message(
                    chat_id, f"🎁 Тебе начислено +{self.settings.referral_bonus} видео за приглашение!"
                )
                try:
                    await self.send_message(
                        ref, f"🎉 По твоей ссылке пришёл друг! +{self.settings.referral_bonus} видео."
                    )
                except Exception:
                    pass
        await self.send_message(chat_id, self.welcome_text())

    async def _send_invite(self, chat_id: int, user_id: int) -> None:
        if not self.bot_username:
            try:
                me = await self._api("getMe")
                self.bot_username = me.get("username", "")
            except Exception:
                pass
        link = f"https://t.me/{self.bot_username}?start={user_id}" if self.bot_username else "(ссылка недоступна)"
        await self.send_message(
            chat_id,
            "👥 Приглашай друзей!\n\n"
            f"За каждого друга ты и он получаете +{self.settings.referral_bonus} видео.\n\n"
            f"Твоя ссылка:\n{link}",
        )

    async def _handle_stats(self, chat_id: int, user_id: int) -> None:
        if not self.settings.admin_user_id or user_id != self.settings.admin_user_id:
            return
        s = get_stats()
        await self.send_message(
            chat_id,
            "📊 Статистика:\n\n"
            f"👤 Пользователей: {s['users']}\n"
            f"🆕 Новых за 24ч: {s['new_24h']}\n"
            f"💎 С подпиской: {s['premium']}\n"
            f"🎬 Видео всего: {s['videos']}\n"
            f"⚙️ Рендеров за 24ч: {s['renders_24h']}\n"
            f"💰 Оплат: {s['pays']}\n\n"
            "Список людей: /users",
        )

    async def _handle_users(self, chat_id: int, user_id: int) -> None:
        if not self.settings.admin_user_id or user_id != self.settings.admin_user_id:
            return
        rows = get_recent_users(30)
        if not rows:
            await self.send_message(chat_id, "Пока никого нет.")
            return
        lines = ["👥 Кто пользовался ботом (последние 30):\n"]
        for i, u in enumerate(rows, 1):
            last = u.get("last_seen")
            last_s = time.strftime("%d.%m %H:%M", time.localtime(last)) if last else "—"
            pro = " 💎" if u.get("is_pro") else ""
            lines.append(
                f"{i}. id {u['user_id']}{pro} · видео {u['videos_used']} · был {last_s}"
            )
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await self.send_message(chat_id, text)

    # ---------- Payments (₽) ----------

    def _pay_manual_keyboard(self, user_id: int) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "✅ Я оплатил", "callback_data": "paid"}],
            ]
        }

    async def _send_invoice(self, chat_id: int, user_id: int) -> None:
        price = self.settings.price_rub
        days = self.settings.sub_days
        title = "PRO-подписка ReelsBot"
        description = f"Безлимит видео с субтитрами на {days} дней."

        if self.settings.has_yookassa:
            try:
                params = {
                    "chat_id": chat_id,
                    "title": title,
                    "description": description,
                    "payload": "sub_pro",
                    "provider_token": self.settings.payment_provider_token,
                    "currency": "RUB",
                    "prices": invoice_prices(price, days),
                    "provider_data": yookassa_provider_data(
                        price, title, self.settings.payment_vat_code
                    ),
                    "need_email": True,
                    "send_email_to_provider": True,
                }
                await self._api("sendInvoice", **params)
                await self.send_message(
                    chat_id,
                    f"💳 Счёт на {price}₽ — оплати картой или СБП.\n"
                    "PRO включится сразу после оплаты.",
                )
                return
            except Exception as exc:
                logger.exception("Invoice failed")
                await self.send_message(
                    chat_id,
                    f"⚠️ Не удалось выставить счёт: {exc}\n\n"
                    "Напиши админу или попробуй позже.",
                )
                if self.settings.payment_info:
                    await self.send_message(
                        chat_id,
                        f"Резервный способ:\n{self.settings.payment_info}",
                        reply_markup=self._pay_manual_keyboard(user_id),
                    )
                return

        await self.send_message(
            chat_id,
            f"💎 PRO-подписка — {price}₽ на {days} дней (безлимит видео).\n\n"
            f"{self.settings.payment_info}",
            reply_markup=self._pay_manual_keyboard(user_id),
        )

    async def _handle_paid_click(self, chat_id: int, user_id: int, username: str) -> None:
        log_event(user_id, "pay_click")
        await self.send_message(
            chat_id,
            "🙏 Спасибо! Пришли, пожалуйста, чек/скрин оплаты сюда. "
            "Как проверю — включу PRO (обычно в течение часа).",
        )
        if self.settings.admin_user_id:
            uname = f"@{username}" if username else "(без username)"
            try:
                await self.send_message(
                    self.settings.admin_user_id,
                    f"💰 Заявка на оплату\nID: {user_id}\nUser: {uname}\n\n"
                    f"Подтвердить: /grant {user_id}",
                )
            except Exception:
                pass

    async def _handle_grant(self, chat_id: int, user_id: int, text: str) -> None:
        if not self.settings.admin_user_id or user_id != self.settings.admin_user_id:
            return
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self.send_message(chat_id, "Использование: /grant <user_id> [дней]")
            return
        target = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else self.settings.sub_days
        set_premium(target, days)
        log_event(target, "pay")
        await self.send_message(chat_id, f"✅ PRO выдан пользователю {target} на {days} дней.")
        try:
            await self.send_message(
                target,
                f"✅ PRO активирован на {days} дней! Теперь безлимит видео. Спасибо! 💎",
            )
        except Exception:
            pass

    async def _handle_pre_checkout(self, query: dict) -> None:
        ok = True
        error_message = ""
        if query.get("invoice_payload") != "sub_pro":
            ok = False
            error_message = "Неверный заказ."
        elif query.get("currency") != "RUB":
            ok = False
            error_message = "Оплата только в рублях."
        elif query.get("total_amount") != self.settings.price_rub * 100:
            ok = False
            error_message = "Неверная сумма."
        try:
            await self._api(
                "answerPreCheckoutQuery",
                pre_checkout_query_id=query["id"],
                ok=ok,
                error_message=error_message,
            )
        except Exception:
            pass

    async def _handle_payment(self, message: dict) -> None:
        user_id = message["from"]["id"]
        chat_id = message["chat"]["id"]
        payment = message.get("successful_payment") or {}
        if payment.get("invoice_payload") != "sub_pro":
            return

        set_premium(user_id, self.settings.sub_days)
        log_event(user_id, "pay")
        charge_id = payment.get("telegram_payment_charge_id", "")

        await self.send_message(
            chat_id,
            f"✅ Оплата {self.settings.price_rub}₽ прошла!\n"
            f"PRO активна на {self.settings.sub_days} дней — безлимит видео. Спасибо! 💎",
        )

        if self.settings.admin_user_id:
            username = message.get("from", {}).get("username", "")
            uname = f"@{username}" if username else "(без username)"
            charge_line = f"Charge: {charge_id[:48]}" if charge_id else ""
            try:
                await self.send_message(
                    self.settings.admin_user_id,
                    "💰 Оплата ЮKassa\n"
                    f"ID: {user_id}\nUser: {uname}\n"
                    f"Сумма: {self.settings.price_rub}₽\n"
                    f"{charge_line}",
                )
            except Exception:
                pass

    # ---------- Video ----------

    async def _process_downloaded_video(
        self, chat_id: int, user_id: int, input_path: Path, work_dir: Path, status_id: int
    ) -> None:
        try:
            segments = await asyncio.to_thread(prepare_segments, self.ai_client, input_path)
            prefs = get_prefs(user_id)
            session = Session(
                video_path=input_path,
                work_dir=work_dir,
                segments=segments,
                style=prefs["style"] if prefs["style"] in STYLES else DEFAULT_STYLE,
                font=prefs["font"] if prefs["font"] in FONTS else DEFAULT_FONT,
                position=prefs["position"] if prefs["position"] in POSITIONS else DEFAULT_POSITION,
                color=prefs["color"] if prefs["color"] in COLORS else DEFAULT_COLOR,
                size=prefs["size"] if prefs["size"] in SIZES else DEFAULT_SIZE,
            )
            self.sessions[user_id] = session
            log_event(user_id, "upload")
            size_mb = input_path.stat().st_size // (1024 * 1024)
            await self.edit_message(
                chat_id, status_id, f"✅ Видео ({size_mb} MB)! Текст распознан. Настрой субтитры 👇"
            )
            await self._send_control_panel(chat_id, session)
        except VideoProcessingError as exc:
            logger.exception("Prepare failed")
            await self.edit_message(chat_id, status_id, f"⚠️ {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as exc:
            logger.exception("Unexpected error")
            await self.edit_message(chat_id, status_id, f"⚠️ Что-то пошло не так: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _handle_video(self, chat_id, user_id, video, document) -> None:
        if not can_process_video(user_id, self.settings.free_video_limit):
            await self.send_message(
                chat_id,
                "😔 Бесплатные видео закончились.\n\n"
                f"💎 Оформи PRO за {self.settings.price_rub}₽/мес — безлимит видео и все стили.\n"
                "👥 Или пригласи друга и получи ещё бесплатно!",
                reply_markup=self._buy_keyboard(),
            )
            return

        if user_id in self.busy:
            await self.send_message(chat_id, "⏳ Уже обрабатываю твоё видео, подожди немного.")
            return

        file_name = "video.mp4"
        if video:
            file_id, file_size = video["file_id"], video.get("file_size") or 0
        else:
            mime = document.get("mime_type") or ""
            if not mime.startswith("video/"):
                await self.send_message(chat_id, "Это не видео. Пришли видеофайл (mp4, mov).")
                return
            file_id = document["file_id"]
            file_name = document.get("file_name") or file_name
            file_size = document.get("file_size") or 0

        limit_mb = self.settings.max_video_size_mb
        if file_size > limit_mb * 1024 * 1024:
            size_mb = file_size // (1024 * 1024)
            url_lim = self.settings.max_url_download_mb
            await self.send_message(
                chat_id,
                f"Видео {size_mb} MB — Telegram не даёт боту скачать больше {limit_mb} MB.\n\n"
                f"🔗 Обход (до {url_lim} MB):\n"
                "1. Залей на Google Drive или Яндекс.Диск\n"
                "2. Открой доступ «всем по ссылке»\n"
                "3. Пришли ссылку сюда — я скачаю сам",
            )
            return

        self._cleanup_session(user_id)
        self.busy.add(user_id)
        size_mb = max(file_size // (1024 * 1024), 1)
        status = await self.send_message(
            chat_id, f"📥 Принял видео ({size_mb} MB). Распознаю речь… подожди."
        )
        status_id = status["message_id"]
        work_dir = Path(tempfile.mkdtemp(prefix="reels-bot-sess-"))
        input_path = work_dir / file_name

        try:
            await self.download_file(file_id, input_path)
            await self._process_downloaded_video(chat_id, user_id, input_path, work_dir, status_id)
        except Exception as exc:
            logger.exception("Download failed")
            await self.edit_message(chat_id, status_id, f"⚠️ Не удалось скачать: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
        finally:
            self.busy.discard(user_id)

    async def _handle_url(self, chat_id: int, user_id: int, url: str) -> None:
        if not can_process_video(user_id, self.settings.free_video_limit):
            await self.send_message(
                chat_id,
                "😔 Бесплатные видео закончились.\n\n"
                f"💎 PRO — {self.settings.price_rub}₽/мес, безлимит: /buy",
                reply_markup=self._buy_keyboard(),
            )
            return

        if user_id in self.busy:
            await self.send_message(chat_id, "⏳ Уже обрабатываю видео, подожди.")
            return

        self._cleanup_session(user_id)
        self.busy.add(user_id)
        status = await self.send_message(chat_id, "🔗 Скачиваю видео по ссылке…")
        status_id = status["message_id"]
        work_dir = Path(tempfile.mkdtemp(prefix="reels-bot-url-"))
        input_path = work_dir / "video.mp4"

        try:
            size = await asyncio.to_thread(
                download_video_url, url, input_path, self.settings.max_url_download_mb
            )
            size_mb = max(size // (1024 * 1024), 1)
            await self.edit_message(
                chat_id, status_id, f"📥 Скачал ({size_mb} MB). Распознаю речь…"
            )
            await self._process_downloaded_video(chat_id, user_id, input_path, work_dir, status_id)
        except UrlDownloadError as exc:
            await self.edit_message(chat_id, status_id, f"⚠️ {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as exc:
            logger.exception("URL download failed")
            await self.edit_message(chat_id, status_id, f"⚠️ Не удалось скачать: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
        finally:
            self.busy.discard(user_id)

    # ---------- Callbacks ----------

    async def _handle_callback(self, callback: dict) -> None:
        callback_id = callback["id"]
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        user_id = callback["from"]["id"]

        if data == "buy":
            await self.answer_callback(callback_id)
            await self._send_invoice(chat_id, user_id)
            return
        if data == "paid":
            await self.answer_callback(callback_id, "Спасибо!")
            username = callback["from"].get("username", "")
            await self._handle_paid_click(chat_id, user_id, username)
            return
        if data == "invite":
            await self.answer_callback(callback_id)
            await self._send_invite(chat_id, user_id)
            return

        session = self.sessions.get(user_id)
        if not session:
            await self.answer_callback(callback_id, "Сессия истекла. Пришли видео заново.")
            return
        session.control_message_id = message_id

        style_title = "🎨 Стиль — как выглядят и двигаются субтитры:\n\n" + "\n".join(
            f"{STYLES[k].label} — {STYLES[k].desc}" for k in STYLE_ORDER
        )
        menus = {
            "menu:style": (style_title, STYLE_ORDER, STYLES, session.style, "style", 2),
            "menu:font": ("🔤 Шрифт — начертание букв. Выбери вайб:", FONT_ORDER, FONTS, session.font, "font", 2),
            "menu:color": ("🌈 Цвет акцентных слов:", COLOR_ORDER, COLORS, session.color, "color", 2),
            "menu:pos": ("📍 Где показывать текст на видео:", POSITION_ORDER, POSITIONS, session.position, "pos", 1),
            "menu:size": ("🔠 Размер текста:", SIZE_ORDER, SIZES, session.size, "size", 3),
        }
        if data in menus:
            title, order, reg, cur, pref, per_row = menus[data]
            await self.answer_callback(callback_id)
            await self.edit_message(chat_id, message_id, title, self._submenu(order, reg, cur, pref, per_row))
            return

        if data == "menu:main":
            await self.answer_callback(callback_id)
            await self.edit_message(chat_id, message_id, self._main_text(session), self._main_keyboard(session))
            return

        setters = {
            "style": (STYLES, "style"),
            "font": (FONTS, "font"),
            "color": (COLORS, "color"),
            "pos": (POSITIONS, "position"),
            "size": (SIZES, "size"),
        }
        if ":" in data:
            prefix, key = data.split(":", 1)
            if prefix in setters:
                registry, attr = setters[prefix]
                if key in registry:
                    setattr(session, attr, key)
                await self.answer_callback(callback_id, "Готово")
                await self.edit_message(chat_id, message_id, self._main_text(session), self._main_keyboard(session))
                return

        if data == "edit:copy":
            await self.answer_callback(callback_id, "Текст ниже")
            await self._send_editable_text(chat_id, session)
            return

        if data == "preview":
            await self.answer_callback(callback_id, "Делаю превью…")
            await self._do_preview(chat_id, user_id, session)
            return

        if data == "render":
            await self.answer_callback(callback_id, "Делаю видео…")
            await self._do_render(chat_id, user_id, session)
            return

        if data == "finish":
            await self.answer_callback(callback_id, "Готово")
            self._cleanup_session(user_id)
            await self.send_message(chat_id, "✨ Готово! Пришли новое видео, когда захочешь.")
            return

        await self.answer_callback(callback_id)

    async def _do_preview(self, chat_id, user_id, session: Session) -> None:
        status = await self.send_message(chat_id, "👁 Готовлю превью…")
        status_id = status["message_id"]
        out_dir = session.work_dir / "prev"
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
            async with self.render_sem:
                preview_path = await asyncio.to_thread(
                    render_preview, session.video_path, session.segments,
                    session.style, session.font, session.position, session.color, out_dir, session.size,
                )
            await self.delete_message(chat_id, status_id)
            # фото БЕЗ меню (на фото кнопки-меню не редактируются)
            await self.send_photo(
                chat_id, preview_path,
                caption="👆 Так будет выглядеть. Меняй настройки ниже или жми «Сделать видео».",
            )
            # свежая панель настроек — на текстовом сообщении, кнопки рабочие
            await self._send_control_panel(chat_id, session)
        except VideoProcessingError as exc:
            await self.edit_message(chat_id, status_id, f"⚠️ {exc}")
        except Exception as exc:
            logger.exception("Preview error")
            await self.edit_message(chat_id, status_id, f"⚠️ Ошибка превью: {exc}")

    async def _do_render(self, chat_id, user_id, session: Session) -> None:
        status = await self.send_message(chat_id, "🎨 Делаю видео… 30–90 сек.")
        status_id = status["message_id"]
        out_dir = session.work_dir / "out"
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
            async with self.render_sem:
                result_path = await asyncio.to_thread(
                    render_segments, session.video_path, session.segments,
                    session.style, session.font, session.position, session.color, out_dir, session.size,
                )
            await self.delete_message(chat_id, status_id)
            caption = (
                f"✅ Готово! {get_style(session.style).label} · {get_font(session.font).label}\n\n"
                "Видео без сжатия — открой как файл.\n"
                "Хочешь другой вариант — поменяй настройки ниже и жми «Сделать видео»."
            )
            # видео БЕЗ меню-кнопок (на видео они не редактируются)
            await self.send_video(chat_id, result_path, caption)

            if not session.counted:
                increment_usage(user_id)
                session.counted = True
                log_event(user_id, "render")
            save_prefs(user_id, session.style, session.font, session.position, session.color, session.size)
            await self.send_message(chat_id, self._status_text(user_id))
            # свежая панель — для нового варианта
            await self._send_control_panel(chat_id, session, self._after_render_keyboard(session))
        except VideoProcessingError as exc:
            await self.edit_message(chat_id, status_id, f"⚠️ {exc}")
        except Exception as exc:
            logger.exception("Render error")
            await self.edit_message(chat_id, status_id, f"⚠️ Что-то пошло не так: {exc}")

    # ---------- Poll ----------

    async def poll(self) -> None:
        await self.set_commands()
        try:
            me = await self._api("getMe")
            self.bot_username = me.get("username", "")
        except Exception:
            pass
        logger.info("Reels bot polling started%s", " | ЮKassa ON" if self.settings.has_yookassa else "")
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self.offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                for update in updates:
                    uid = update["update_id"]
                    self.offset = uid + 1
                    if uid in self._seen_updates:
                        continue
                    self._seen_updates.add(uid)
                    if len(self._seen_updates) > 1000:
                        self._seen_updates = set(list(self._seen_updates)[-500:])
                    await self.handle_update(update)
            except Exception:
                logger.exception("Polling error")
                await asyncio.sleep(3)

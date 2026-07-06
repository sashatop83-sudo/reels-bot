import asyncio
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from bot.config import Settings, create_groq_client
from bot.db import (
    add_bonus,
    can_process_video,
    get_prefs,
    get_stats,
    increment_usage,
    log_event,
    remaining_videos,
    save_prefs,
    set_premium,
    try_set_referrer,
)
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
    DEFAULT_STYLE,
    FONT_ORDER,
    FONTS,
    POSITION_ORDER,
    POSITIONS,
    STYLE_ORDER,
    STYLES,
    get_color,
    get_font,
    get_position,
    get_style,
)
from bot.services.transcribe import SubtitleSegment

logger = logging.getLogger(__name__)

MAX_VIDEO_SIZE_MB = 20
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
    awaiting_edit: bool = False
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

    # ---------- Telegram API ----------

    async def _api(self, method: str, **params):
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{self.base_url}/{method}", json=params)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram API error"))
            return data["result"]

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
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
        await self._send_file("sendVideo", "video", chat_id, path, caption, reply_markup, "video/mp4")

    async def send_photo(self, chat_id, path, caption="", reply_markup=None):
        await self._send_file("sendPhoto", "photo", chat_id, path, caption, reply_markup, "image/png")

    async def download_file(self, file_id: str, destination: Path) -> None:
        file_info = await self._api("getFile", file_id=file_id)
        url = f"https://api.telegram.org/file/bot{self.settings.telegram_bot_token}/{file_info['file_path']}"
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            destination.write_bytes(resp.content)

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

    # ---------- Keyboards ----------

    def _main_keyboard(self, session: Session) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": f"🎨 Стиль: {get_style(session.style).label}", "callback_data": "menu:style"},
                ],
                [
                    {"text": f"🔤 Шрифт: {get_font(session.font).label}", "callback_data": "menu:font"},
                    {"text": f"🌈 Цвет", "callback_data": "menu:color"},
                ],
                [
                    {"text": f"📍 {get_position(session.position).label}", "callback_data": "menu:pos"},
                    {"text": "✏️ Текст", "callback_data": "edit"},
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
        return {
            "inline_keyboard": [
                [{"text": f"💎 PRO — {self.settings.price_rub}₽ / {self.settings.sub_days} дней", "callback_data": "buy"}],
                [{"text": "👥 Пригласить друга (+видео)", "callback_data": "invite"}],
            ]
        }

    # ---------- Text ----------

    def _clean(self, text: str) -> str:
        return text.replace("*", "")

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
            f"📍 Позиция: {get_position(session.position).label}\n\n"
            "💡 Ошибка в слове? Напиши в чат: неправильное = правильное\n"
            "👁 Превью покажет кадр, 🎬 сделает видео."
        )

    def welcome_text(self) -> str:
        return (
            "👋 Привет! Я делаю видео с крутыми Reels-субтитрами.\n\n"
            "1️⃣ Пришли видео (до 20 MB)\n"
            "2️⃣ Я распознаю речь\n"
            "3️⃣ Выберешь стиль, шрифт, цвет, позицию\n"
            "4️⃣ Поправишь слова при желании\n"
            "5️⃣ Получишь готовое видео 🔥\n\n"
            "✨ Умею: подсветку слов, авто-вертикаль, улучшение звука.\n"
            f"🎁 Бесплатно: {self.settings.free_video_limit} видео на пробу.\n"
            f"💎 Дальше PRO — {self.settings.price_rub}₽/мес (безлимит). Или зови друзей — дам ещё!\n\n"
            "Пришли видео, чтобы начать 👇"
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
            replaced = sum(self._replace_word(session, f, r) for f, r in word_edits)
            changed = 0
            for idx, new_text in line_edits.items():
                if 1 <= idx <= len(session.segments):
                    session.segments[idx - 1].text = new_text
                    session.segments[idx - 1].words = []
                    changed += 1
            reports = []
            if replaced:
                reports.append(f"заменил слов: {replaced}")
            if changed:
                reports.append(f"изменил строк: {changed}")
            return "Готово — " + ", ".join(reports) + "." if reports else "Не нашёл что заменить."

        cleaned = [re.sub(r"^\s*\d+\.\s*", "", ln) for ln in plain_lines]
        if not cleaned:
            return "Не понял правки. Пришли «слово = правильное» или весь текст списком."
        count = min(len(cleaned), len(session.segments))
        for i in range(count):
            session.segments[i].text = cleaned[i]
            session.segments[i].words = []
        return f"Текст обновлён ({count} строк)."

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
        if text.startswith("/grant"):
            await self._handle_grant(chat_id, user_id, text)
            return

        if message.get("video") or message.get("document"):
            await self._handle_video(chat_id, user_id, message.get("video"), message.get("document"))
            return

        if text and not text.startswith("/"):
            session = self.sessions.get(user_id)
            first_line = text.splitlines()[0] if text.splitlines() else ""
            if session and (session.awaiting_edit or PAIR_RE.match(first_line)):
                result = self._apply_edits(session, text)
                session.awaiting_edit = False
                await self.send_message(chat_id, result)
                control = await self.send_message(
                    chat_id, self._main_text(session), reply_markup=self._main_keyboard(session)
                )
                session.control_message_id = control["message_id"]
                return

        await self.send_message(chat_id, "Пришли видео — я сделаю Reels-субтитры 🎬")

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
            f"💰 Оплат: {s['pays']}",
        )

    # ---------- Payments (₽) ----------

    def _pay_manual_keyboard(self, user_id: int) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "✅ Я оплатил", "callback_data": "paid"}],
            ]
        }

    async def _send_invoice(self, chat_id: int, user_id: int) -> None:
        # Если подключён платёжный провайдер (ЮKassa через BotFather) — счёт в рублях
        if self.settings.payment_provider_token:
            try:
                await self._api(
                    "sendInvoice",
                    chat_id=chat_id,
                    title="PRO-подписка ReelsBot",
                    description=f"Безлимит субтитров на {self.settings.sub_days} дней.",
                    payload="sub_pro",
                    provider_token=self.settings.payment_provider_token,
                    currency="RUB",
                    prices=[{"label": f"PRO {self.settings.sub_days} дн.", "amount": self.settings.price_rub * 100}],
                )
                return
            except Exception as exc:
                logger.exception("Invoice failed")
                await self.send_message(chat_id, f"Не удалось создать счёт: {exc}")

        # Ручная оплата (без юрлица): реквизиты + подтверждение админом
        await self.send_message(
            chat_id,
            f"💎 PRO-подписка — {self.settings.price_rub}₽ на {self.settings.sub_days} дней (безлимит видео).\n\n"
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
        try:
            await self._api("answerPreCheckoutQuery", pre_checkout_query_id=query["id"], ok=True)
        except Exception:
            pass

    async def _handle_payment(self, message: dict) -> None:
        user_id = message["from"]["id"]
        chat_id = message["chat"]["id"]
        set_premium(user_id, self.settings.sub_days)
        log_event(user_id, "pay")
        await self.send_message(
            chat_id,
            f"✅ Оплата прошла! PRO активна на {self.settings.sub_days} дней. Спасибо! 💎\nТеперь безлимит видео.",
        )

    # ---------- Video ----------

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

        if file_size > MAX_VIDEO_SIZE_MB * 1024 * 1024:
            await self.send_message(chat_id, f"Видео слишком большое. Максимум {MAX_VIDEO_SIZE_MB} MB.")
            return

        self._cleanup_session(user_id)
        self.busy.add(user_id)
        status = await self.send_message(chat_id, "📥 Принял видео. Распознаю речь… 30–90 сек.")
        status_id = status["message_id"]
        work_dir = Path(tempfile.mkdtemp(prefix="reels-bot-sess-"))
        input_path = work_dir / file_name

        try:
            await self.download_file(file_id, input_path)
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
            )
            self.sessions[user_id] = session
            log_event(user_id, "upload")

            await self.edit_message(chat_id, status_id, "✅ Текст распознан!")
            control = await self.send_message(
                chat_id, self._main_text(session), reply_markup=self._main_keyboard(session)
            )
            session.control_message_id = control["message_id"]
        except VideoProcessingError as exc:
            logger.exception("Prepare failed")
            await self.edit_message(chat_id, status_id, f"⚠️ {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as exc:
            logger.exception("Unexpected error")
            await self.edit_message(chat_id, status_id, f"⚠️ Что-то пошло не так: {exc}")
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

        menus = {
            "menu:style": ("🎨 Выбери стиль:", STYLE_ORDER, STYLES, session.style, "style", 2),
            "menu:font": ("🔤 Выбери шрифт:", FONT_ORDER, FONTS, session.font, "font", 2),
            "menu:color": ("🌈 Выбери цвет:", COLOR_ORDER, COLORS, session.color, "color", 2),
            "menu:pos": ("📍 Где показывать текст:", POSITION_ORDER, POSITIONS, session.position, "pos", 1),
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

        if data == "edit":
            session.awaiting_edit = True
            await self.answer_callback(callback_id, "Жду новый текст")
            await self.send_message(
                chat_id,
                "✏️ Как поправить текст:\n\n"
                "🔹 Заменить слово (проще всего):\n"
                "   неправильное = правильное\n"
                "   Пример: будет = помог\n\n"
                "🔹 Несколько замен — каждую с новой строки\n"
                "🔹 Строку целиком: номер = новый текст\n"
                "🔹 Или пришли весь текст заново списком.",
            )
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
                    session.style, session.font, session.position, session.color, out_dir,
                )
            await self.edit_message(chat_id, status_id, "Вот как будет выглядеть 👇")
            await self.send_photo(
                chat_id, preview_path,
                caption="Нравится? Жми 🎬 «Сделать видео». Или поменяй настройки.",
                reply_markup=self._main_keyboard(session),
            )
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
                    session.style, session.font, session.position, session.color, out_dir,
                )
            await self.edit_message(chat_id, status_id, "📤 Готово! Отправляю…")
            caption = (
                f"✅ Готово! {get_style(session.style).label} · {get_font(session.font).label}\n\n"
                "Хочешь другой вариант — поменяй настройки и жми «Сделать видео»."
            )
            await self.send_video(chat_id, result_path, caption, self._after_render_keyboard(session))

            if not session.counted:
                increment_usage(user_id)
                session.counted = True
                log_event(user_id, "render")
            save_prefs(user_id, session.style, session.font, session.position, session.color)
            await self.send_message(chat_id, self._status_text(user_id))
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
        logger.info("Reels bot polling started")
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self.offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.handle_update(update)
            except Exception:
                logger.exception("Polling error")
                await asyncio.sleep(3)

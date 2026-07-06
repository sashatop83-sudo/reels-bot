import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    groq_api_key: str
    free_video_limit: int
    admin_user_id: int
    price_rub: int
    sub_days: int
    referral_bonus: int
    max_concurrent_renders: int
    payment_provider_token: str
    payment_info: str


def create_groq_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


DEFAULT_PAYMENT_INFO = (
    "Переведи 99₽ по СБП на номер +7XXXXXXXXXX (укажи в .env → PAYMENT_INFO).\n"
    "После оплаты пришли чек сюда и нажми «✅ Я оплатил»."
)


def get_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    api_key = os.getenv("GROQ_API_KEY", "").strip()

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

    return Settings(
        telegram_bot_token=token,
        groq_api_key=api_key,
        free_video_limit=_int_env("FREE_VIDEO_LIMIT", 2),
        admin_user_id=_int_env("ADMIN_USER_ID", 0),
        price_rub=_int_env("PRICE_RUB", 99),
        sub_days=_int_env("SUB_DAYS", 30),
        referral_bonus=_int_env("REFERRAL_BONUS", 3),
        max_concurrent_renders=_int_env("MAX_CONCURRENT_RENDERS", 2),
        payment_provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip(),
        payment_info=os.getenv("PAYMENT_INFO", "").strip() or DEFAULT_PAYMENT_INFO,
    )

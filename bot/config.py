import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
TELEGRAM_MAX_FILE_MB = 20          # без Local Bot API
TELEGRAM_LOCAL_MAX_FILE_MB = 2000  # с Local Bot API


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
    max_video_size_mb: int
    max_url_download_mb: int
    telegram_api_id: str
    telegram_api_hash: str
    telegram_api_url: str
    payment_provider_token: str
    payment_vat_code: int
    payment_tax_system_code: int
    payment_info: str

    @property
    def has_yookassa(self) -> bool:
        return bool(self.payment_provider_token.strip())

    @property
    def uses_local_api(self) -> bool:
        return bool(self.telegram_api_url.strip())


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

    api_url = os.getenv("TELEGRAM_API_URL", "").strip()
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()

    # если есть ID/HASH но нет URL — start.sh поднимет local API на 8081
    if not api_url and api_id and api_hash:
        api_url = "http://127.0.0.1:8081"

    cap = TELEGRAM_LOCAL_MAX_FILE_MB if api_url else TELEGRAM_MAX_FILE_MB
    default_mb = 200 if api_url else TELEGRAM_MAX_FILE_MB
    max_video = min(_int_env("MAX_VIDEO_SIZE_MB", default_mb), cap)
    max_url = min(_int_env("MAX_URL_DOWNLOAD_MB", 1024), 2048)

    return Settings(
        telegram_bot_token=token,
        groq_api_key=api_key,
        free_video_limit=_int_env("FREE_VIDEO_LIMIT", 2),
        admin_user_id=_int_env("ADMIN_USER_ID", 0),
        price_rub=_int_env("PRICE_RUB", 99),
        sub_days=_int_env("SUB_DAYS", 30),
        referral_bonus=_int_env("REFERRAL_BONUS", 3),
        max_concurrent_renders=_int_env("MAX_CONCURRENT_RENDERS", 1),
        max_video_size_mb=max_video,
        max_url_download_mb=max_url,
        telegram_api_id=api_id,
        telegram_api_hash=api_hash,
        telegram_api_url=api_url,
        payment_provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip(),
        payment_vat_code=_int_env("PAYMENT_VAT_CODE", 1),
        payment_tax_system_code=_int_env("PAYMENT_TAX_SYSTEM_CODE", 2),
        payment_info=os.getenv("PAYMENT_INFO", "").strip() or DEFAULT_PAYMENT_INFO,
    )

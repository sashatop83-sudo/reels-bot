"""Скачивание файлов: стандартный API (20 MB) или Local Bot API (до 2 GB)."""

from pathlib import Path

import httpx

from bot.config import Settings


def api_base(settings: Settings) -> str:
    if settings.telegram_api_url:
        return f"{settings.telegram_api_url.rstrip('/')}/bot{settings.telegram_bot_token}"
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}"


def file_url(settings: Settings, file_path: str) -> str:
    if settings.telegram_api_url:
        base = settings.telegram_api_url.rstrip("/")
        return f"{base}/file/bot{settings.telegram_bot_token}/{file_path}"
    return f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"


async def download_telegram_file(settings: Settings, file_id: str, destination: Path) -> None:
    base = api_base(settings)
    timeout = max(300.0, settings.max_video_size_mb * 3)  # ~3 сек на MB
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base}/getFile", json={"file_id": file_id})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getFile failed"))
        file_path = data["result"]["file_path"]
        url = file_url(settings, file_path)
        dl = await client.get(url)
        dl.raise_for_status()
        destination.write_bytes(dl.content)

"""Скачивание видео по ссылке — обход лимита Telegram 20 MB."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
VIDEO_MIMES = ("video/", "application/octet-stream")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_OPEN_RE = re.compile(r"drive\.google\.com/(?:open|uc)\?[^#]*[?&]id=([a-zA-Z0-9_-]+)")
GDRIVE_ID_RE = re.compile(r"[?&]id=([a-zA-Z0-9_-]+)")


class UrlDownloadError(Exception):
    pass


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text.strip())
    if not match:
        return None
    return match.group(0).rstrip(".,;)")


def _gdrive_file_id(url: str) -> str | None:
    for pattern in (GDRIVE_FILE_RE, GDRIVE_OPEN_RE, GDRIVE_ID_RE):
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _resolve_gdrive(url: str) -> str:
    file_id = _gdrive_file_id(url)
    if not file_id:
        raise UrlDownloadError("Не понял ссылку Google Drive. Нужна ссылка вида drive.google.com/file/d/...")
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"


def _resolve_dropbox(url: str) -> str:
    parsed = urlparse(url)
    if "dropbox.com" not in parsed.netloc:
        raise UrlDownloadError("Это не Dropbox.")
    out = url.replace("?dl=0", "?dl=1")
    if "dl=1" not in out:
        out += "&dl=1" if "?" in out else "?dl=1"
    return out


def _resolve_yandex(url: str) -> str:
    if "disk.yandex" not in url and "yadi.sk" not in url:
        raise UrlDownloadError("Это не Яндекс.Диск.")
    public_key = quote(url, safe="")
    api = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={public_key}"
    try:
        resp = httpx.get(api, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise UrlDownloadError(f"Яндекс.Диск не отдал ссылку: {exc}") from exc
    href = data.get("href")
    if not href:
        raise UrlDownloadError("Яндекс.Диск: ссылка недоступна. Проверь, что файл открыт по ссылке.")
    return href


def resolve_download_url(url: str) -> str:
    lower = url.lower()
    if "drive.google.com" in lower or "docs.google.com" in lower:
        return _resolve_gdrive(url)
    if "dropbox.com" in lower:
        return _resolve_dropbox(url)
    if "disk.yandex" in lower or "yadi.sk" in lower:
        return _resolve_yandex(url)
    path = unquote(urlparse(url).path).lower()
    if any(path.endswith(ext) for ext in VIDEO_SUFFIXES):
        return url
    if "video" in lower or "download" in lower or "cdn" in lower:
        return url
    raise UrlDownloadError(
        "Поддерживаю: Google Drive, Яндекс.Диск, Dropbox или прямую ссылку на .mp4/.mov"
    )


def _filename_from_response(resp: httpx.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    match = re.search(r'filename[*]?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        if name:
            return name
    path = urlparse(url).path
    if path and "/" in path:
        candidate = Path(unquote(path.split("/")[-1])).name
        if candidate and "." in candidate:
            return candidate
    return "video.mp4"


def _looks_like_video(resp: httpx.Response, filename: str) -> bool:
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if any(ctype.startswith(m) for m in VIDEO_MIMES):
        return True
    if any(filename.lower().endswith(ext) for ext in VIDEO_SUFFIXES):
        return True
    if "google" in ctype or "html" in ctype or "text" in ctype:
        return False
    # если тип неизвестен, но размер большой — скорее видео
    cl = resp.headers.get("content-length")
    if cl and int(cl) > 500_000:
        return True
    return False


def _gdrive_stream_url(client: httpx.Client, file_id: str) -> str:
    base = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp = client.get(base)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/html" not in ctype:
        return base

    token_match = re.search(r"confirm=([0-9A-Za-z_]+)", resp.text)
    token = token_match.group(1) if token_match else "t"
    for cookie in resp.cookies.jar:
        if cookie.name.startswith("download_warning"):
            token = cookie.value
            break
    return f"{base}&confirm={token}"


def download_video_url(url: str, destination: Path, max_mb: int) -> int:
    """Скачать видео по ссылке. Возвращает размер в байтах."""
    direct = resolve_download_url(url)
    max_bytes = max_mb * 1024 * 1024
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        stream_url = direct
        if "drive.google.com" in url.lower():
            file_id = _gdrive_file_id(url)
            if file_id:
                stream_url = _gdrive_stream_url(client, file_id)

        with client.stream("GET", stream_url) as resp:
            resp.raise_for_status()
            filename = _filename_from_response(resp, stream_url)
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/html" in ctype:
                raise UrlDownloadError(
                    "Файл недоступен. Открой доступ «всем по ссылке» и попробуй снова."
                )
            if not _looks_like_video(resp, filename):
                raise UrlDownloadError(
                    "По ссылке не видео. Залей mp4/mov и открой доступ по ссылке."
                )
            cl = resp.headers.get("content-length")
            if cl and int(cl) > max_bytes:
                raise UrlDownloadError(
                    f"Файл {int(cl) // (1024 * 1024)} MB — больше лимита {max_mb} MB."
                )

            total = 0
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        destination.unlink(missing_ok=True)
                        raise UrlDownloadError(f"Файл больше {max_mb} MB.")
                    fh.write(chunk)

    if total < 50_000:
        destination.unlink(missing_ok=True)
        raise UrlDownloadError(
            "Скачалось слишком мало — возможно, ссылка ведёт на страницу, а не на файл. "
            "Проверь доступ «всем по ссылке»."
        )
    return total

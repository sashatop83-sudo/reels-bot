"""Скачивание видео по ссылке — Google Drive, Яндекс.Диск, Dropbox."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_ID_RE = re.compile(r"[?&]id=([a-zA-Z0-9_-]+)")


class UrlDownloadError(Exception):
    pass


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text.strip())
    if not match:
        return None
    return match.group(0).rstrip(".,;)")


def extract_url_from_message(text: str, entities: list | None) -> str | None:
    found = extract_url(text or "")
    if found:
        return found
    if not entities:
        return None
    for ent in entities:
        if ent.get("type") != "url":
            continue
        offset = ent.get("offset", 0)
        length = ent.get("length", 0)
        part = (text or "")[offset : offset + length]
        found = extract_url(part)
        if found:
            return found
    return None


def _clean_share_url(url: str) -> str:
    parsed = urlparse(url.strip())
    # убираем utm и прочий мусор — API Яндекса/GDrive чувствительны
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _expand_short_url(client: httpx.Client, url: str) -> str:
    if "yadi.sk" not in url:
        return url
    try:
        resp = client.get(url, follow_redirects=True)
        return str(resp.url)
    except Exception:
        return url


def _gdrive_file_id(url: str) -> str | None:
    for pattern in (GDRIVE_FILE_RE, GDRIVE_ID_RE):
        match = pattern.search(url)
        if match:
            return match.group(1)
    if "drive.google.com" in url:
        qs = parse_qs(urlparse(url).query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
    return None


def _is_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head


def _is_video_header(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[4:8] == b"ftyp":  # mp4 / mov
        return True
    if data[:4] == b"\x1aE\xdf\xa3":  # mkv / webm
        return True
    return False


def _verify_video_file(path: Path) -> None:
    with path.open("rb") as fh:
        head = fh.read(64)
    if _is_html(head):
        raise UrlDownloadError(
            "Скачалась страница сайта, а не видео.\n\n"
            "Поделись ссылкой именно на файл (не папку). "
            "Или скачай mp4 и отправь файлом до 20 MB."
        )
    if not _is_video_header(head):
        raise UrlDownloadError("По ссылке не видеофайл (нужен mp4/mov).")


def _stream_to_file(client: httpx.Client, stream_url: str, destination: Path, max_bytes: int) -> int:
    timeout = httpx.Timeout(connect=45.0, read=600.0, write=30.0, pool=30.0)
    with client.stream("GET", stream_url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        cl = resp.headers.get("content-length")
        if cl and int(cl) > max_bytes:
            raise UrlDownloadError(f"Файл {int(cl) // (1024 * 1024)} MB — больше лимита {max_bytes // (1024 * 1024)} MB.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        first_chunk = b""
        with destination.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                if not chunk:
                    continue
                if not first_chunk:
                    first_chunk = chunk[:512]
                    if _is_html(first_chunk):
                        raise UrlDownloadError("html_page")
                total += len(chunk)
                if total > max_bytes:
                    destination.unlink(missing_ok=True)
                    raise UrlDownloadError(f"Файл больше {max_bytes // (1024 * 1024)} MB.")
                fh.write(chunk)

    if total < 10_000:
        destination.unlink(missing_ok=True)
        raise UrlDownloadError("tiny_file")
    _verify_video_file(destination)
    return total


def _gdrive_download_urls(client: httpx.Client, file_id: str) -> list[str]:
    urls = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
        f"https://docs.google.com/uc?export=download&id={file_id}&confirm=t",
    ]
    try:
        base = f"https://drive.google.com/uc?export=download&id={file_id}"
        resp = client.get(base, timeout=30.0)
        if resp.status_code == 200:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/html" in ctype:
                token = "t"
                m = re.search(r"confirm=([0-9A-Za-z_-]+)", resp.text)
                if m:
                    token = m.group(1)
                for cookie in resp.cookies.jar:
                    if cookie.name.startswith("download_warning"):
                        token = cookie.value
                        break
                urls.insert(0, f"{base}&confirm={token}")
            elif "text/html" not in ctype:
                urls.insert(0, base)
    except Exception:
        pass
    return urls


def _download_gdrive(client: httpx.Client, url: str, destination: Path, max_bytes: int) -> int:
    file_id = _gdrive_file_id(url)
    if not file_id:
        raise UrlDownloadError(
            "Не понял ссылку Google Drive.\n"
            "Нужна ссылка на файл: drive.google.com/file/d/…/view"
        )
    last_err: Exception | None = None
    for stream_url in _gdrive_download_urls(client, file_id):
        try:
            return _stream_to_file(client, stream_url, destination, max_bytes)
        except UrlDownloadError as exc:
            last_err = exc
            if str(exc) not in ("html_page", "tiny_file"):
                raise
            destination.unlink(missing_ok=True)
        except Exception as exc:
            last_err = exc
            destination.unlink(missing_ok=True)

    raise UrlDownloadError(
        "Google Drive не отдал видео.\n\n"
        "Проверь:\n"
        "• Ссылка на файл, не на папку\n"
        "• Доступ: «Все, у кого есть ссылка»\n"
        "• Или скачай mp4 и отправь **файлом** в бота"
    ) from last_err


def _download_yandex(client: httpx.Client, url: str, destination: Path, max_bytes: int) -> int:
    url = _clean_share_url(_expand_short_url(client, url))

    if "/d/" in url and "/i/" not in url:
        try:
            meta = client.get(
                "https://cloud-api.yandex.net/v1/disk/public/resources",
                params={"public_key": url, "limit": 1},
                timeout=30.0,
            )
            if meta.status_code == 200:
                info = meta.json()
                if info.get("type") == "dir":
                    raise UrlDownloadError(
                        "Это ссылка на папку, а нужна на видеофайл.\n\n"
                        "Яндекс.Диск: открой файл → Поделиться → Скопировать ссылку"
                    )
        except UrlDownloadError:
            raise
        except Exception:
            pass

    try:
        resp = client.get(
            "https://cloud-api.yandex.net/v1/disk/public/resources/download",
            params={"public_key": url},
            timeout=60.0,
        )
        resp.raise_for_status()
        href = resp.json().get("href")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (403, 404):
            raise UrlDownloadError(
                "Яндекс.Диск не открыл файл.\n\n"
                "• Открой файл (не папку) → Поделиться\n"
                "• «Скопировать ссылку» / доступ по ссылке\n"
                "• Пришли ссылку вида disk.yandex.ru/i/…"
            ) from exc
        raise UrlDownloadError(f"Яндекс.Диск ошибка {code}") from exc
    except Exception as exc:
        raise UrlDownloadError(f"Яндекс.Диск: {exc}") from exc

    if not href:
        raise UrlDownloadError("Яндекс.Диск не вернул ссылку на скачивание.")

    try:
        return _stream_to_file(client, href, destination, max_bytes)
    except UrlDownloadError as exc:
        if str(exc) in ("html_page", "tiny_file"):
            raise UrlDownloadError(
                "Яндекс.Диск отдал не видео.\n"
                "Проверь, что ссылка на mp4/mov файл, не на папку."
            ) from exc
        raise


def _download_dropbox(client: httpx.Client, url: str, destination: Path, max_bytes: int) -> int:
    stream_url = url.replace("?dl=0", "?dl=1")
    if "dl=1" not in stream_url:
        stream_url += "&dl=1" if "?" in stream_url else "?dl=1"
    return _stream_to_file(client, stream_url, destination, max_bytes)


def _download_direct(client: httpx.Client, url: str, destination: Path, max_bytes: int) -> int:
    return _stream_to_file(client, url, destination, max_bytes)


def download_video_url(url: str, destination: Path, max_mb: int) -> int:
    url = url.strip()
    lower = url.lower()
    max_bytes = max_mb * 1024 * 1024

    with httpx.Client(headers=UA, follow_redirects=True) as client:
        if "drive.google.com" in lower or "docs.google.com" in lower:
            return _download_gdrive(client, url, destination, max_bytes)
        if "disk.yandex" in lower or "yadi.sk" in lower:
            return _download_yandex(client, url, destination, max_bytes)
        if "dropbox.com" in lower:
            return _download_dropbox(client, url, destination, max_bytes)

        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in VIDEO_SUFFIXES):
            return _download_direct(client, url, destination, max_bytes)

        raise UrlDownloadError(
            "Не понял ссылку.\n\n"
            "Поддерживаю:\n"
            "• Google Drive (на файл)\n"
            "• Яндекс.Диск disk.yandex.ru/i/…\n"
            "• Dropbox\n"
            "• Прямая ссылка на .mp4"
        )

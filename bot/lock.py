"""Эксклюзивная блокировка polling — только одна копия бота слушает Telegram."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from bot.db import DB_PATH

_lock_handle = None


def acquire_polling_lock() -> bool:
    global _lock_handle
    lock_path = DB_PATH.parent / "bot.poll.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return True

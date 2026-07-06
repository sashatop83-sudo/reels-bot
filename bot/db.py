import os
import sqlite3
import time
from pathlib import Path

_default_db = Path(__file__).resolve().parent.parent / "data" / "bot.db"
DB_PATH = Path(os.getenv("DB_PATH", "").strip() or _default_db)

USER_COLUMNS = {
    "videos_used": "INTEGER NOT NULL DEFAULT 0",
    "is_premium": "INTEGER NOT NULL DEFAULT 0",
    "premium_until": "REAL NOT NULL DEFAULT 0",
    "bonus_videos": "INTEGER NOT NULL DEFAULT 0",
    "referred_by": "INTEGER",
    "pref_style": "TEXT",
    "pref_font": "TEXT",
    "pref_position": "TEXT",
    "pref_color": "TEXT",
    "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for col, ddl in USER_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        conn.commit()


def get_user(user_id: int) -> sqlite3.Row:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row


def is_active_premium(user: sqlite3.Row) -> bool:
    if user["is_premium"]:
        return True
    return bool(user["premium_until"] and user["premium_until"] > time.time())


def can_process_video(user_id: int, free_limit: int) -> bool:
    user = get_user(user_id)
    if is_active_premium(user):
        return True
    allowance = free_limit + (user["bonus_videos"] or 0)
    return (user["videos_used"] or 0) < allowance


def increment_usage(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET videos_used = COALESCE(videos_used,0) + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def remaining_videos(user_id: int, free_limit: int) -> int | str:
    user = get_user(user_id)
    if is_active_premium(user):
        return "безлимит"
    allowance = free_limit + (user["bonus_videos"] or 0)
    return max(allowance - (user["videos_used"] or 0), 0)


def set_premium(user_id: int, days: int) -> None:
    until = time.time() + days * 86400
    with _connect() as conn:
        current = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        base = time.time()
        if current and current["premium_until"] and current["premium_until"] > base:
            base = current["premium_until"]
        conn.execute(
            "UPDATE users SET premium_until = ? WHERE user_id = ?",
            (base + days * 86400, user_id),
        )
        conn.commit()


def add_bonus(user_id: int, count: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET bonus_videos = COALESCE(bonus_videos,0) + ? WHERE user_id = ?",
            (count, user_id),
        )
        conn.commit()


def try_set_referrer(user_id: int, referrer_id: int, bonus: int) -> bool:
    """Привязать реферера один раз. Возвращает True, если засчитано (награждаем обоих)."""
    if user_id == referrer_id:
        return False
    with _connect() as conn:
        user = conn.execute(
            "SELECT referred_by, videos_used FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if user is None:
            conn.execute(
                "INSERT INTO users (user_id, referred_by) VALUES (?, ?)",
                (user_id, referrer_id),
            )
        elif user["referred_by"]:
            return False
        else:
            conn.execute(
                "UPDATE users SET referred_by = ? WHERE user_id = ?",
                (referrer_id, user_id),
            )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (referrer_id,)
        )
        conn.execute(
            "UPDATE users SET bonus_videos = COALESCE(bonus_videos,0) + ? WHERE user_id = ?",
            (bonus, referrer_id),
        )
        conn.execute(
            "UPDATE users SET bonus_videos = COALESCE(bonus_videos,0) + ? WHERE user_id = ?",
            (bonus, user_id),
        )
        conn.commit()
        return True


def save_prefs(user_id: int, style: str, font: str, position: str, color: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET pref_style = ?, pref_font = ?, pref_position = ?, pref_color = ?
            WHERE user_id = ?
            """,
            (style, font, position, color, user_id),
        )
        conn.commit()


def get_prefs(user_id: int) -> dict:
    user = get_user(user_id)
    return {
        "style": user["pref_style"],
        "font": user["pref_font"],
        "position": user["pref_position"],
        "color": user["pref_color"],
    }


def log_event(user_id: int | None, event_type: str) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events (user_id, type, ts) VALUES (?, ?, ?)",
                (user_id, event_type, time.time()),
            )
            conn.commit()
    except Exception:
        pass


def get_stats() -> dict:
    with _connect() as conn:
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        premium = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE is_premium = 1 OR premium_until > ?",
            (time.time(),),
        ).fetchone()["c"]
        total_videos = conn.execute(
            "SELECT COALESCE(SUM(videos_used),0) s FROM users"
        ).fetchone()["s"]
        day_ago = time.time() - 86400
        renders_24h = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='render' AND ts > ?", (day_ago,)
        ).fetchone()["c"]
        new_24h = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='start' AND ts > ?", (day_ago,)
        ).fetchone()["c"]
        pays = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='pay'"
        ).fetchone()["c"]
        return {
            "users": total_users,
            "premium": premium,
            "videos": total_videos,
            "renders_24h": renders_24h,
            "new_24h": new_24h,
            "pays": pays,
        }

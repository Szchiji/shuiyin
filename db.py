"""SQLite persistence layer for the watermark Telegram bot."""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "watermark_bot.db")

_DB_DIR = os.path.dirname(DB_PATH)
if _DB_DIR:
    os.makedirs(_DB_DIR, exist_ok=True)

LOGO_DIR = os.path.join(_DB_DIR, "user_logos") if _DB_DIR else "user_logos"

DAILY_LIMIT = 3
CONTACT_DAILY_LIMIT = 10


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT    NOT NULL DEFAULT '',
                first_name    TEXT    NOT NULL DEFAULT '',
                role          TEXT    NOT NULL DEFAULT 'regular',
                member_until  TEXT,
                daily_count   INTEGER NOT NULL DEFAULT 0,
                last_reset    TEXT    NOT NULL DEFAULT '',
                web_token     TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN web_token TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        try:
            conn.execute("ALTER TABLE users ADD COLUMN web_token_expires_at TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        for col_sql in (
            "ALTER TABLE users ADD COLUMN contact_text TEXT",
            "ALTER TABLE users ADD COLUMN contact_daily_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN contact_last_reset TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watermark_settings (
                user_id    INTEGER PRIMARY KEY,
                wm_type    TEXT    NOT NULL DEFAULT 'text',
                text       TEXT    NOT NULL DEFAULT '© Wei',
                logo_path  TEXT,
                position   TEXT    NOT NULL DEFAULT '右下',
                opacity    INTEGER NOT NULL DEFAULT 75,
                tiled      INTEGER NOT NULL DEFAULT 0,
                font_size  INTEGER NOT NULL DEFAULT 5,
                logo_scale INTEGER NOT NULL DEFAULT 20
            )
        """)
        try:
            conn.execute("ALTER TABLE watermark_settings ADD COLUMN font_size INTEGER NOT NULL DEFAULT 5")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        try:
            conn.execute("ALTER TABLE watermark_settings ADD COLUMN logo_scale INTEGER NOT NULL DEFAULT 20")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        defaults = {
            "default_text": "© Wei",
            "default_position": "右下",
            "default_opacity": "75",
            "default_font_size": "5",
            "default_tiled": "0",
            "daily_limit": str(DAILY_LIMIT),
            "contact_default_text": "你好，想和你沟通一下，方便回复吗？",
            "contact_daily_limit": "10",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
                (k, v),
            )


def ensure_user(user_id: int, username: str = "", first_name: str = "") -> None:
    """Create the user if missing. Only overwrite name fields when new values are non-empty."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username or "", first_name or ""),
        )
        if username or first_name:
            row = conn.execute(
                "SELECT username, first_name FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            new_username = username if username else (row["username"] if row else "")
            new_first = first_name if first_name else (row["first_name"] if row else "")
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (new_username, new_first, user_id),
            )


def get_user(user_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_effective_role(user_id: int, admin_ids: set) -> str:
    if user_id in admin_ids:
        return "admin"
    u = get_user(user_id)
    if not u:
        return "regular"
    if u["role"] == "member" and u["member_until"]:
        if u["member_until"] >= date.today().isoformat():
            return "member"
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET role='regular', member_until=NULL WHERE user_id=?",
                (user_id,),
            )
    return "regular"


def _get_daily_limit() -> int:
    settings = get_system_settings()
    try:
        return max(1, int(settings.get("daily_limit", DAILY_LIMIT)))
    except (ValueError, TypeError):
        return DAILY_LIMIT


def get_daily_usage(user_id: int) -> tuple[int, int]:
    today = date.today().isoformat()
    limit = _get_daily_limit()
    u = get_user(user_id)
    if not u or u["last_reset"] != today:
        return 0, limit
    return u["daily_count"], limit


def check_and_increment_usage(user_id: int) -> tuple[bool, int]:
    today = date.today().isoformat()
    limit = _get_daily_limit()
    with _conn() as conn:
        row = conn.execute(
            "SELECT daily_count, last_reset FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return False, 0
        count = row["daily_count"] if row["last_reset"] == today else 0
        if count >= limit:
            return False, 0
        conn.execute(
            "UPDATE users SET daily_count=?, last_reset=? WHERE user_id=?",
            (count + 1, today, user_id),
        )
        return True, limit - count - 1


def refund_usage(user_id: int) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT daily_count, last_reset FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row or row["last_reset"] != today or row["daily_count"] <= 0:
            return
        conn.execute(
            "UPDATE users SET daily_count=? WHERE user_id=?",
            (row["daily_count"] - 1, user_id),
        )


def _get_contact_daily_limit() -> int:
    settings = get_system_settings()
    try:
        return max(1, int(settings.get("contact_daily_limit", CONTACT_DAILY_LIMIT)))
    except (ValueError, TypeError):
        return CONTACT_DAILY_LIMIT


def get_contact_usage(user_id: int) -> tuple[int, int]:
    today = date.today().isoformat()
    limit = _get_contact_daily_limit()
    u = get_user(user_id)
    if not u or u.get("contact_last_reset") != today:
        return 0, limit
    return int(u.get("contact_daily_count") or 0), limit


def check_and_increment_contact_usage(user_id: int) -> tuple[bool, int]:
    today = date.today().isoformat()
    limit = _get_contact_daily_limit()
    with _conn() as conn:
        row = conn.execute(
            "SELECT contact_daily_count, contact_last_reset FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return False, 0
        count = row["contact_daily_count"] if row["contact_last_reset"] == today else 0
        count = int(count or 0)
        if count >= limit:
            return False, 0
        conn.execute(
            "UPDATE users SET contact_daily_count=?, contact_last_reset=? WHERE user_id=?",
            (count + 1, today, user_id),
        )
        return True, limit - count - 1


def get_contact_text(user_id: int) -> str:
    u = get_user(user_id)
    personal = (u or {}).get("contact_text") or ""
    if str(personal).strip():
        return str(personal).strip()
    settings = get_system_settings()
    return (settings.get("contact_default_text") or "你好，想和你沟通一下，方便回复吗？").strip()


def save_contact_text(user_id: int, text: str) -> None:
    ensure_user(user_id)
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET contact_text=? WHERE user_id=?",
            (text.strip(), user_id),
        )


def save_system_contact_text(text: str) -> None:
    save_system_settings(contact_default_text=text.strip())


def add_member(user_id: int, days: int) -> str:
    until = (date.today() + timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET role='member', member_until=? WHERE user_id=?",
            (until, user_id),
        )
    return until


def revoke_member(user_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET role='regular', member_until=NULL WHERE user_id=?",
            (user_id,),
        )


_DEFAULTS: dict = {
    "wm_type": "text",
    "text": "© Wei",
    "logo_path": None,
    "position": "右下",
    "opacity": 75,
    "tiled": 0,
    "font_size": 5,
    "logo_scale": 20,
}


def get_watermark_settings(user_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM watermark_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else {"user_id": user_id, **_DEFAULTS}


def save_watermark_settings(user_id: int, **kwargs) -> None:
    current = get_watermark_settings(user_id)
    current.update(kwargs)
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO watermark_settings
                (user_id, wm_type, text, logo_path, position, opacity, tiled, font_size, logo_scale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                current["wm_type"],
                current["text"],
                current.get("logo_path"),
                current["position"],
                current["opacity"],
                current["tiled"],
                current.get("font_size", 5),
                current.get("logo_scale", 20),
            ),
        )


def get_stats() -> dict:
    today = date.today().isoformat()
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='member' AND member_until >= ?",
            (today,),
        ).fetchone()[0]
        active_today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_reset=? AND daily_count > 0",
            (today,),
        ).fetchone()[0]
    return {"total": total, "members": members, "regulars": total - members, "active_today": active_today}


_WEB_TOKEN_EXPIRY_DAYS = 7


def generate_web_token(user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=_WEB_TOKEN_EXPIRY_DAYS)).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET web_token=?, web_token_expires_at=? WHERE user_id=?",
            (token, expires_at, user_id),
        )
    return token


def get_user_by_token(token: str) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE web_token=? AND web_token_expires_at > ?",
            (token, now),
        ).fetchone()
        return dict(row) if row else None


def list_users(page: int = 1, limit: int = 20, search: str = "") -> tuple[list[dict], int]:
    offset = (page - 1) * limit
    with _conn() as conn:
        if search:
            pattern = f"%{search}%"
            total = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?",
                (pattern, pattern, pattern),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ? "
                "ORDER BY user_id LIMIT ? OFFSET ?",
                (pattern, pattern, pattern, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM users ORDER BY user_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows], total


def get_system_settings() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_system_settings(**kwargs) -> None:
    with _conn() as conn:
        for k, v in kwargs.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                (k, str(v)),
            )

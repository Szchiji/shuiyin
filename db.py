"""SQLite persistence layer for the watermark Telegram bot."""

import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "watermark_bot.db")

# Ensure the directory that holds the SQLite file exists. On platforms such as
# Railway, DB_PATH usually points into a mounted volume (e.g.
# "/data/watermark_bot.db"). sqlite3 cannot create missing parent directories
# and would fail with "unable to open database file", which silently breaks
# every write — including saving a watermark template. Creating it up-front
# makes persistence work as soon as DB_PATH is configured.
_DB_DIR = os.path.dirname(DB_PATH)
if _DB_DIR:
    os.makedirs(_DB_DIR, exist_ok=True)

# Directory for user-uploaded logo template images. Co-locate it with the
# database file so that logo (image) watermark templates persist on the same
# volume as DB_PATH. When DB_PATH has no directory component (local default),
# fall back to the historical relative "user_logos" directory.
LOGO_DIR = os.path.join(_DB_DIR, "user_logos") if _DB_DIR else "user_logos"

DAILY_LIMIT = 3  # free-tier daily usage cap


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        # Migrate existing installations: add web_token column if missing.
        # SQLite raises OperationalError with "duplicate column name" when the
        # column already exists; re-raise for any other unexpected error.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN web_token TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        # Migrate: add web_token_expires_at column if missing.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN web_token_expires_at TEXT")
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
        # Migrate existing installations: add font_size column if missing.
        try:
            conn.execute("ALTER TABLE watermark_settings ADD COLUMN font_size INTEGER NOT NULL DEFAULT 5")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
        # Migrate existing installations: add logo_scale column if missing.
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
        # Insert defaults if table is empty
        defaults = {
            "default_text": "© Wei",
            "default_position": "右下",
            "default_opacity": "75",
            "default_font_size": "5",
            "default_tiled": "0",
            "daily_limit": str(DAILY_LIMIT),
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
                (k, v),
            )


# ── User helpers ──────────────────────────────────────────────────────────────

def ensure_user(user_id: int, username: str = "", first_name: str = "") -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name),
        )
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )


def get_user(user_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_effective_role(user_id: int, admin_ids: set) -> str:
    """Return 'admin', 'member', or 'regular'. Auto-expires outdated memberships."""
    if user_id in admin_ids:
        return "admin"
    u = get_user(user_id)
    if not u:
        return "regular"
    if u["role"] == "member" and u["member_until"]:
        if u["member_until"] >= date.today().isoformat():
            return "member"
        # Membership expired — downgrade
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET role='regular', member_until=NULL WHERE user_id=?",
                (user_id,),
            )
    return "regular"


# ── Usage counting ────────────────────────────────────────────────────────────

def _get_daily_limit() -> int:
    """Return the configured daily usage limit from system_settings."""
    settings = get_system_settings()
    try:
        return max(1, int(settings.get("daily_limit", DAILY_LIMIT)))
    except (ValueError, TypeError):
        return DAILY_LIMIT


def get_daily_usage(user_id: int) -> tuple[int, int]:
    """Return (used_today, daily_limit)."""
    today = date.today().isoformat()
    limit = _get_daily_limit()
    u = get_user(user_id)
    if not u or u["last_reset"] != today:
        return 0, limit
    return u["daily_count"], limit


def check_and_increment_usage(user_id: int) -> tuple[bool, int]:
    """
    If the user has remaining quota, increment counter and return (True, remaining_after).
    Otherwise return (False, 0).
    """
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


# ── Membership management ─────────────────────────────────────────────────────

def add_member(user_id: int, days: int) -> str:
    """Grant membership for *days* days starting today. Returns expiry ISO date."""
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


# ── Watermark settings ────────────────────────────────────────────────────────

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


# ── Admin stats ───────────────────────────────────────────────────────────────

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


# ── Web token ─────────────────────────────────────────────────────────────────

_WEB_TOKEN_EXPIRY_DAYS = 7


def generate_web_token(user_id: int) -> str:
    """Generate (or refresh) a random web-login token for *user_id*. Returns the token."""
    token = secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=_WEB_TOKEN_EXPIRY_DAYS)).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET web_token=?, web_token_expires_at=? WHERE user_id=?",
            (token, expires_at, user_id),
        )
    return token


def get_user_by_token(token: str) -> dict | None:
    """Return the user row whose web_token matches and has not expired, or None."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE web_token=? AND web_token_expires_at > ?",
            (token, now),
        ).fetchone()
        return dict(row) if row else None


# ── User listing ──────────────────────────────────────────────────────────────

def list_users(page: int = 1, limit: int = 20, search: str = "") -> tuple[list[dict], int]:
    """Return (rows, total_count) for the given page."""
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


# ── System settings ───────────────────────────────────────────────────────────

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

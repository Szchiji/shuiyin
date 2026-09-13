"""Build Telegram prefill-chat links from forwarded messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?:\?|/|$)",
    re.IGNORECASE,
)


@dataclass
class ContactTarget:
    display_name: str = ""
    username: str | None = None
    user_id: int | None = None
    phone: str | None = None
    hidden: bool = False
    origin: str = ""


def extract_forward_target(message) -> ContactTarget | None:
    if message is None:
        return None
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is not None:
            return ContactTarget(
                display_name=_display_name(sender),
                username=getattr(sender, "username", None) or None,
                user_id=getattr(sender, "id", None),
                origin="user",
            )
        hidden_name = getattr(origin, "sender_user_name", None)
        if hidden_name:
            return ContactTarget(display_name=hidden_name, hidden=True, origin="hidden")
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is not None:
            title = getattr(chat, "title", None) or getattr(chat, "full_name", "") or "频道/群组"
            return ContactTarget(display_name=title, origin="channel")
        return ContactTarget(origin="unknown")
    if getattr(message, "forward_from", None):
        u = message.forward_from
        return ContactTarget(
            display_name=_display_name(u),
            username=u.username or None,
            user_id=u.id,
            origin="user",
        )
    if getattr(message, "forward_sender_name", None):
        return ContactTarget(display_name=message.forward_sender_name, hidden=True, origin="hidden")
    if getattr(message, "forward_from_chat", None):
        chat = message.forward_from_chat
        return ContactTarget(display_name=getattr(chat, "title", None) or "频道/群组", origin="channel")
    return None


def extract_reply_user(message) -> ContactTarget | None:
    if message is None or message.reply_to_message is None:
        return None
    src = message.reply_to_message
    if src.from_user and not src.from_user.is_bot:
        u = src.from_user
        return ContactTarget(
            display_name=_display_name(u),
            username=u.username or None,
            user_id=u.id,
            origin="user",
        )
    return None


def parse_username_or_link(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = raw.lstrip("@")
    m = _TME_RE.search(raw)
    if m:
        raw = m.group(1)
    if raw.startswith("+") and raw[1:].isdigit() and 8 <= len(raw) <= 16:
        return raw
    if _USERNAME_RE.match(raw):
        return raw
    return None


def fill_template(template: str, target: ContactTarget, me_name: str = "") -> str:
    text = template or ""
    text = text.replace("{name}", target.display_name or "")
    text = text.replace("{username}", target.username or "")
    text = text.replace("{me}", me_name or "")
    text = text.replace("{id}", str(target.user_id) if target.user_id else "")
    text = text.strip()
    if text.startswith("@"):
        text = " " + text
    return text[:500]


def build_prefill_url(target: ContactTarget, draft: str) -> str | None:
    encoded = quote(draft, safe="")
    if target.username:
        return f"https://t.me/{target.username}?text={encoded}"
    if target.phone:
        phone = target.phone if target.phone.startswith("+") else f"+{target.phone}"
        return f"https://t.me/{phone}?text={encoded}"
    return None


def format_success_html(target: ContactTarget, url: str, draft: str = "") -> str:
    name = _esc(target.display_name or "对方")
    lines = [f"对方：{name}"]
    if target.username:
        lines[0] += f"  @{_esc(target.username)}"
    if target.user_id:
        lines.append(f"ID：<code>{target.user_id}</code>")
    lines.append("")
    if draft:
        lines.append("预填文案：")
        lines.append(f"<code>{_esc(draft)}</code>")
        lines.append("")
    lines.append("点下方链接打开私聊，文案已预填，需自己点发送。")
    lines.append("")
    lines.append(url)
    return "\n".join(lines)


def format_partial_html(target: ContactTarget, draft: str) -> str:
    name = _esc(target.display_name or "对方")
    lines = [f"对方：{name}"]
    if target.user_id:
        lines.append(f"ID：<code>{target.user_id}</code>")
    lines.append("")
    lines.append("对方没有公开用户名，无法生成预填链接。可复制下面文案，自己打开私聊发送：")
    lines.append(f"<code>{_esc(draft)}</code>")
    return "\n".join(lines)


def is_forwarded_update(update) -> bool:
    msg = update.effective_message
    if msg is None:
        return False
    return extract_forward_target(msg) is not None


def _display_name(user) -> str:
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    return f"{first} {last}".strip() or "对方"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

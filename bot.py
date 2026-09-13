"""
Telegram 水印机器人

角色说明：
  👑 管理员 (admin)  — 通过环境变量 ADMIN_IDS 配置，无使用限制
  ⭐ 会员   (member) — 管理员授权，在有效期内无限制使用
  👤 普通用户        — 每天最多 3 次，可联系管理员购买会员

必填环境变量：
  BOT_TOKEN    — Telegram 机器人 Token
  ADMIN_IDS    — 管理员 Telegram 用户 ID（多个用英文逗号分隔）

可选环境变量：
  ADMIN_USERNAME — 显示给用户的管理员联系方式（如 myname）
"""

import asyncio
import logging
import os
import pathlib
import tempfile
import urllib.parse

import contact
import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

POSITIONS = ["左上", "右上", "左下", "右下", "居中", "中上", "中下"]
LOGO_DIR = db.LOGO_DIR

ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}


def _role(user_id: int) -> str:
    return db.get_effective_role(user_id, ADMIN_IDS)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _admin_contact() -> str:
    uname = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    primary = next(iter(ADMIN_IDS), None)
    return f"管理员（ID: {primary}）" if primary else "管理员"
  # ── Watermark display helpers ─────────────────────────────────────────────────

def _wm_summary(s: dict) -> str:
    type_label = "🖼 图片水印" if s["wm_type"] == "logo" else "📝 文字水印"
    logo_ok = s.get("logo_path") and os.path.exists(s["logo_path"])
    lines = ["⚙️ 当前水印模板：", f"• 类型：{type_label}"]
    if s["wm_type"] == "logo":
        lines.append(f"• 图片：{'✅ 已上传' if logo_ok else '❌ 尚未上传'}")
        lines.append(f"• 大小：{s.get('logo_scale', 20)}%")
    else:
        lines.append(f"• 文字：{s['text']}")
        lines.append(f"• 字号：{s.get('font_size', 5)}%")
    lines += [
        f"• 位置：{s['position']}",
        f"• 透明度：{s['opacity']}%",
        f"• 平铺：{'开' if s['tiled'] else '关'}",
    ]
    return "\n".join(lines)


def _settings_kb(s: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎨 修改水印模板", callback_data="set_template")],
        [InlineKeyboardButton(f"📍 位置: {s['position']}", callback_data="set_position")],
    ]
    if s.get("wm_type") == "logo":
        rows.append([InlineKeyboardButton(f"📐 大小: {s.get('logo_scale', 20)}%", callback_data="set_logo_scale")])
    else:
        rows.append([InlineKeyboardButton(f"🔤 字号: {s.get('font_size', 5)}%", callback_data="set_font_size")])
    rows += [
        [InlineKeyboardButton(f"🔆 透明度: {s['opacity']}%", callback_data="set_opacity")],
        [InlineKeyboardButton(f"🔲 平铺: {'开' if s['tiled'] else '关'}", callback_data="toggle_tiled")],
    ]
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    role = _role(user.id)
    role_label = {"admin": "👑 管理员", "member": "⭐ 会员", "regular": "👤 普通用户"}[role]
    text = (
        f"👋 你好，{user.first_name}！欢迎使用水印机器人。\n"
        f"身份：{role_label}\n\n"
        "📌 水印：\n"
        "1️⃣ /template — 设置水印模板（文字或图片）\n"
        "2️⃣ /settings — 调整位置、透明度、平铺等参数\n"
        "3️⃣ 直接发送图片或视频，机器人自动添加水印返回\n\n"
        "📌 私信预填：\n"
        "把对方的消息转发给我，我会返回可复制 ID 和预填链接。\n"
        "/contact — 查看当前预填文案\n"
        "/contact_tpl — 设置你的预填文案\n\n"
        "4️⃣ /webtoken — 获取网页后台一键登录链接\n\n"
    )
    if role == "regular":
        used, limit = db.get_daily_usage(user.id)
        c_used, c_limit = db.get_contact_usage(user.id)
        text += (
            f"⏳ 今日水印：{used}/{limit} 次\n"
            f"⏳ 今日预填链接：{c_used}/{c_limit} 次\n"
            f"💬 购买会员请联系 {_admin_contact()}"
        )
    elif role == "member":
        u = db.get_user(user.id)
        text += f"📅 会员到期：{u['member_until']}"
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    role = _role(user.id)
    u = db.get_user(user.id)
    lines = [f"👤 {user.first_name}（ID: {user.id}）"]
    if role == "admin":
        lines.append("身份：👑 管理员")
    elif role == "member":
        lines.append(f"身份：⭐ 会员（到期：{u['member_until']}）")
    else:
        used, limit = db.get_daily_usage(user.id)
        c_used, c_limit = db.get_contact_usage(user.id)
        lines += [
            "身份：👤 普通用户",
            f"今日水印：{used}/{limit} 次",
            f"今日预填链接：{c_used}/{c_limit} 次",
            f"购买会员请联系 {_admin_contact()}",
        ]
    s = db.get_watermark_settings(user.id)
    lines += ["", _wm_summary(s)]
    await update.message.reply_text("\n".join(lines))


async def cmd_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 文字水印", callback_data="template_text")],
        [InlineKeyboardButton("🖼 图片水印（Logo）", callback_data="template_logo")],
    ])
    await update.message.reply_text(
        "请选择水印模板类型：\n\n"
        "📝 文字水印 — 输入任意文字（支持中文/英文/emoji）\n"
        "🖼 图片水印 — 上传 Logo 图片（PNG 透明背景效果更佳）",
        reply_markup=kb,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    s = db.get_watermark_settings(user.id)
    await update.message.reply_text(_wm_summary(s), reply_markup=_settings_kb(s))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    role = _role(user.id)
    text = (
        "📖 命令列表：\n\n"
        "/start — 欢迎页，查看身份和使用说明\n"
        "/template — 设置水印模板（文字 或 图片 Logo）\n"
        "/settings — 调整水印位置、透明度、平铺\n"
        "/status — 查看当前身份、使用次数和水印设置\n"
        "/contact — 查看私信预填说明和当前文案\n"
        "/contact_tpl — 设置个人预填文案\n"
        "/link — 群内回复某人消息，生成预填链接\n"
        "/webtoken — 获取网页后台一键登录链接\n"
        "/help — 显示此帮助\n\n"
        "📷 直接发送图片或视频即可添加水印\n"
        "↪️ 转发一条消息即可生成预填私聊链接\n"
    )
    if role == "regular":
        text += f"\n💬 购买会员（无限次）请联系 {_admin_contact()}"
    if role == "admin":
        text += (
            "\n👑 管理员命令：\n"
            "/addmember <用户ID> <天数> — 授权会员\n"
            "/revokemember <用户ID> — 撤销会员\n"
            "/userinfo <用户ID> — 查询用户信息\n"
            "/stats — 查看统计数据\n"
            "/contact_set — 设置系统默认预填文案\n"
        )
    await update.message.reply_text(text)

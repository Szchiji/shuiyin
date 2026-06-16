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

import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
LOGO_DIR = "user_logos"

# ── Admin IDs (loaded once at import time) ────────────────────────────────────

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


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    role = _role(user.id)

    role_label = {"admin": "👑 管理员", "member": "⭐ 会员", "regular": "👤 普通用户"}[role]
    text = (
        f"👋 你好，{user.first_name}！欢迎使用水印机器人。\n"
        f"身份：{role_label}\n\n"
        "📌 使用方法：\n"
        "1️⃣ /template — 设置水印模板（文字或图片）\n"
        "2️⃣ /settings — 调整位置、透明度、平铺等参数\n"
        "3️⃣ 直接发送图片或视频，机器人自动添加水印返回\n"
        "4️⃣ /webtoken — 获取网页后台一键登录链接\n\n"
    )
    if role == "regular":
        used, limit = db.get_daily_usage(user.id)
        text += (
            f"⏳ 今日已用：{used}/{limit} 次\n"
            f"💬 购买会员（无限次）请联系 {_admin_contact()}"
        )
    elif role == "member":
        u = db.get_user(user.id)
        text += f"📅 会员到期：{u['member_until']}"
    await update.message.reply_text(text)


# ── /status ───────────────────────────────────────────────────────────────────

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
        lines += [
            "身份：👤 普通用户",
            f"今日已用：{used}/{limit} 次",
            f"购买会员请联系 {_admin_contact()}",
        ]

    s = db.get_watermark_settings(user.id)
    lines += ["", _wm_summary(s)]
    await update.message.reply_text("\n".join(lines))


# ── /template ─────────────────────────────────────────────────────────────────

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


# ── /settings ─────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    s = db.get_watermark_settings(user.id)
    await update.message.reply_text(_wm_summary(s), reply_markup=_settings_kb(s))


# ── /help ─────────────────────────────────────────────────────────────────────

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
        "/webtoken — 获取网页后台一键登录链接\n"
        "/help — 显示此帮助\n\n"
        "📷 直接发送图片或视频即可添加水印\n"
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
        )
    await update.message.reply_text(text)


# ── Admin commands ────────────────────────────────────────────────────────────

async def cmd_addmember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 此命令仅限管理员使用。")
        return
    args = context.args or []
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await update.message.reply_text(
            "用法：/addmember <用户ID> <天数>\n例：/addmember 123456789 30"
        )
        return
    target_id = int(args[0])
    days = int(args[1])
    db.ensure_user(target_id)
    until = db.add_member(target_id, days)
    await update.message.reply_text(
        f"✅ 已为用户 {target_id} 授权 {days} 天会员\n到期日：{until}"
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 恭喜！管理员已为你开通 {days} 天会员（到期：{until}）\n"
                "现在你可以无限制使用水印功能！"
            ),
        )
    except Exception:
        pass  # User may not have started the bot yet


async def cmd_revokemember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 此命令仅限管理员使用。")
        return
    args = context.args or []
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("用法：/revokemember <用户ID>")
        return
    target_id = int(args[0])
    db.revoke_member(target_id)
    await update.message.reply_text(f"✅ 已撤销用户 {target_id} 的会员资格。")


async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 此命令仅限管理员使用。")
        return
    args = context.args or []
    if len(args) != 1 or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("用法：/userinfo <用户ID>")
        return
    target_id = int(args[0])
    u = db.get_user(target_id)
    if not u:
        await update.message.reply_text(
            f"❌ 未找到用户 {target_id}，该用户可能从未启动过机器人。"
        )
        return
    role = db.get_effective_role(target_id, ADMIN_IDS)
    s = db.get_watermark_settings(target_id)
    lines = [
        f"👤 用户信息",
        f"ID: {u['user_id']}",
        f"名字: {u['first_name'] or '—'}",
        f"用户名: {'@' + u['username'] if u['username'] else '—'}",
        f"角色: {role}",
        f"会员到期: {u['member_until'] or '无'}",
        f"今日使用: {u['daily_count']} 次",
        "",
        _wm_summary(s),
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ 此命令仅限管理员使用。")
        return
    stats = db.get_stats()
    text = (
        "📊 机器人统计：\n"
        f"总用户数：{stats['total']}\n"
        f"当前会员数：{stats['members']}\n"
        f"普通用户数：{stats['regulars']}\n"
        f"今日活跃：{stats['active_today']}\n"
    )
    await update.message.reply_text(text)


# ── /webtoken ─────────────────────────────────────────────────────────────────

async def cmd_webtoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.first_name or "")
    token = db.generate_web_token(user.id)
    web_url = os.getenv("WEB_URL", "http://localhost:8000").rstrip("/")
    login_url = f"{web_url}/autologin?token={urllib.parse.quote(token, safe='')}"
    role = db.get_effective_role(user.id, ADMIN_IDS)
    role_label = {"admin": "👑 管理员", "member": "⭐ 会员", "regular": "👤 普通用户"}.get(role, "👤 普通用户")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 一键登录后台", url=login_url)]])
    await update.message.reply_text(
        f"🔑 点击下方按钮即可自动登录后台，无需输入任何密码。\n\n"
        f"当前身份：{role_label}\n\n"
        "⚠️ 链接仅供本人使用，请勿分享给他人。每次发送此命令会刷新链接。",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "set_template":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 文字水印", callback_data="template_text")],
            [InlineKeyboardButton("🖼 图片水印（Logo）", callback_data="template_logo")],
        ])
        await query.edit_message_text("请选择水印模板类型：", reply_markup=kb)

    elif data == "template_text":
        context.user_data["awaiting"] = "template_text"
        await query.edit_message_text("📝 请发送水印文字内容（支持中文/英文/emoji）：")

    elif data == "template_logo":
        context.user_data["awaiting"] = "template_logo"
        await query.edit_message_text(
            "🖼 请发送水印 Logo 图片：\n\n"
            "• 推荐 PNG 格式（透明背景）\n"
            "• 建议以「文件」方式发送以保持清晰度\n"
            "• 普通发送图片也可以"
        )

    elif data == "set_position":
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(p, callback_data=f"pos_{p}")] for p in POSITIONS]
        )
        await query.edit_message_text("📍 请选择水印位置：", reply_markup=kb)

    elif data.startswith("pos_"):
        new_pos = data[4:]
        db.save_watermark_settings(user_id, position=new_pos)
        s = db.get_watermark_settings(user_id)
        await query.edit_message_text(
            f"✅ 位置已设为：{new_pos}\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )

    elif data == "set_opacity":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{v}%", callback_data=f"opacity_{v}") for v in [25, 50, 75, 100]]
        ])
        await query.edit_message_text("🔆 请选择水印透明度：", reply_markup=kb)

    elif data.startswith("opacity_"):
        new_opacity = int(data.split("_")[1])
        db.save_watermark_settings(user_id, opacity=new_opacity)
        s = db.get_watermark_settings(user_id)
        await query.edit_message_text(
            f"✅ 透明度已设为：{new_opacity}%\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )

    elif data == "toggle_tiled":
        s = db.get_watermark_settings(user_id)
        new_tiled = 0 if s["tiled"] else 1
        db.save_watermark_settings(user_id, tiled=new_tiled)
        s = db.get_watermark_settings(user_id)
        await query.edit_message_text(
            f"✅ 平铺已{'开启' if new_tiled else '关闭'}。\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )

    elif data == "set_logo_scale":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{v}%", callback_data=f"logo_scale_{v}") for v in [10, 15, 20, 25]],
            [InlineKeyboardButton(f"{v}%", callback_data=f"logo_scale_{v}") for v in [30, 40, 50, 60]],
            [InlineKeyboardButton(f"{v}%", callback_data=f"logo_scale_{v}") for v in [70, 80, 90, 100]],
        ])
        await query.edit_message_text("📐 请选择图片水印大小（占图片短边的百分比）：", reply_markup=kb)

    elif data.startswith("logo_scale_"):
        new_scale = int(data.split("_")[2])
        db.save_watermark_settings(user_id, logo_scale=new_scale)
        s = db.get_watermark_settings(user_id)
        await query.edit_message_text(
            f"✅ 图片水印大小已设为：{new_scale}%\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )

    elif data == "set_font_size":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{v}%", callback_data=f"font_size_{v}") for v in [3, 5, 7, 10]],
            [InlineKeyboardButton(f"{v}%", callback_data=f"font_size_{v}") for v in [13, 15, 20, 25]],
        ])
        await query.edit_message_text("🔤 请选择文字水印字号（占图片高度的百分比）：", reply_markup=kb)

    elif data.startswith("font_size_"):
        parts = data.split("_")
        if len(parts) != 3 or not parts[2].isdigit():
            await query.answer("无效操作", show_alert=True)
            return
        new_size = int(parts[2])
        db.save_watermark_settings(user_id, font_size=new_size)
        s = db.get_watermark_settings(user_id)
        await query.edit_message_text(
            f"✅ 字号已设为：{new_size}%\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )


# ── Text handler ──────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    awaiting = context.user_data.get("awaiting")

    if awaiting == "template_text":
        new_text = update.message.text.strip()
        if not new_text:
            await update.message.reply_text("❌ 水印文字不能为空，请重新输入：")
            return
        db.save_watermark_settings(user_id, wm_type="text", text=new_text)
        context.user_data.pop("awaiting", None)
        s = db.get_watermark_settings(user_id)
        await update.message.reply_text(
            f"✅ 文字水印已设置：「{new_text}」\n\n{_wm_summary(s)}",
            reply_markup=_settings_kb(s),
        )
    else:
        await update.message.reply_text(
            "发送图片或视频来添加水印。\n"
            "使用 /template 设置水印模板，/settings 调整参数。"
        )


# ── Logo upload handler ───────────────────────────────────────────────────────

async def _save_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the sent image and save it as this user's logo template."""
    user_id = update.effective_user.id
    msg = await update.message.reply_text("⏳ 正在保存水印图片…")
    try:
        if update.message.document:
            tg_file = await update.message.document.get_file()
            fname = update.message.document.file_name or "logo.png"
            ext = pathlib.Path(fname).suffix.lower() or ".png"
        else:
            # compressed photo from Telegram — save as jpg
            tg_file = await update.message.photo[-1].get_file()
            ext = ".jpg"

        os.makedirs(LOGO_DIR, exist_ok=True)
        logo_path = os.path.join(LOGO_DIR, f"{user_id}{ext}")
        await tg_file.download_to_drive(logo_path)

        db.save_watermark_settings(user_id, wm_type="logo", logo_path=logo_path)
        context.user_data.pop("awaiting", None)
        s = db.get_watermark_settings(user_id)
        await msg.edit_text(f"✅ 图片水印已保存！\n\n{_wm_summary(s)}")
        await update.message.reply_text(
            "现在发送照片或视频，机器人会自动叠加此 Logo 水印。\n"
            "使用 /settings 可调整位置、透明度等。",
            reply_markup=_settings_kb(s),
        )
    except Exception as e:
        logger.error("保存 logo 失败: %s", e, exc_info=True)
        await msg.edit_text(f"❌ 保存失败：{e}")


# ── Main media watermark handler ──────────────────────────────────────────────

async def _apply_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download media, apply saved watermark template, return result."""
    from main import add_watermark_to_image, add_watermark_to_video  # noqa: PLC0415

    user = update.effective_user
    user_id = user.id
    db.ensure_user(user_id, user.username or "", user.first_name or "")
    role = _role(user_id)

    # ── Determine file type first (validate before charging quota) ────────────
    is_video = False
    ext = "jpg"
    tg_file = None

    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()
    elif update.message.video:
        fname = update.message.video.file_name or "video.mp4"
        ext = pathlib.Path(fname).suffix.lstrip(".").lower() or "mp4"
        if ext not in {"mp4", "mov"}:
            await update.message.reply_text("❌ 仅支持 mp4/mov 视频格式。")
            return
        tg_file = await update.message.video.get_file()
        is_video = True
    elif update.message.document:
        doc = update.message.document
        fname = doc.file_name or "file"
        ext = pathlib.Path(fname).suffix.lstrip(".").lower()
        if ext in {"mp4", "mov"}:
            is_video = True
        elif ext not in {"jpg", "jpeg", "png", "webp"}:
            await update.message.reply_text("❌ 仅支持图片（jpg/png/webp）或视频（mp4/mov）。")
            return
        tg_file = await doc.get_file()
    else:
        await update.message.reply_text("❌ 请发送图片或视频文件。")
        return

    # ── Check / charge daily quota for regular users ──────────────────────────
    limit_note = ""
    if role == "regular":
        allowed, remaining = db.check_and_increment_usage(user_id)
        if not allowed:
            await update.message.reply_text(
                "❌ 今日使用次数已达上限（3次/天）。\n"
                f"💬 购买会员（无限次）请联系 {_admin_contact()}。"
            )
            return
        if remaining == 0:
            limit_note = "\n⚠️ 今日次数已用完，明天再来吧！"
        elif remaining == 1:
            limit_note = "\n（今日剩余 1 次）"

    # ── Resolve watermark params from saved template ──────────────────────────
    s = db.get_watermark_settings(user_id)
    wm_type = s.get("wm_type", "text")
    logo_path = s.get("logo_path") if wm_type == "logo" else None
    # Verify stored logo file still exists; fall back to text if missing
    if logo_path and not os.path.exists(logo_path):
        logo_path = None
        wm_type = "text"
    text = s.get("text", "© Wei")

    # ── Process ───────────────────────────────────────────────────────────────
    msg = await update.message.reply_text("⏳ 正在处理，请稍候…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, f"input.{ext}")
            output_ext = "mp4" if is_video else "jpg"
            output_path = os.path.join(tmpdir, f"output.{output_ext}")

            await tg_file.download_to_drive(input_path)

            loop = asyncio.get_running_loop()
            if is_video:
                success = await loop.run_in_executor(
                    None,
                    add_watermark_to_video,
                    input_path, output_path, text,
                    s["position"], s["opacity"], bool(s["tiled"]), logo_path,
                    None, None, s.get("font_size", 5), s.get("logo_scale", 20),
                )
            else:
                success = await loop.run_in_executor(
                    None,
                    add_watermark_to_image,
                    input_path, output_path, text,
                    s["position"], s["opacity"], bool(s["tiled"]), logo_path,
                    None, None, s.get("font_size", 5), s.get("logo_scale", 20),
                )

            if not success:
                await msg.edit_text("❌ 处理失败，请重试。")
                return

            await msg.delete()
            wm_label = "图片水印" if wm_type == "logo" else f"水印：{text}"
            caption_out = f"✅ {wm_label}{limit_note}"
            if is_video:
                with open(output_path, "rb") as f:
                    await update.message.reply_video(f, caption=caption_out)
            else:
                with open(output_path, "rb") as f:
                    await update.message.reply_photo(f, caption=caption_out)

    except Exception as e:
        logger.error("处理媒体失败: %s", e, exc_info=True)
        # The status message may already be deleted (e.g. the failure happened
        # while sending the result) or unreachable (network timeout), so editing
        # it can raise again. Fall back to a fresh reply and never let the error
        # handler itself crash the update processing.
        try:
            await msg.edit_text(f"❌ 处理失败：{e}")
        except Exception:
            try:
                await update.message.reply_text(f"❌ 处理失败：{e}")
            except Exception:
                logger.error("无法向用户发送处理失败提示", exc_info=True)


# ── Unified photo / document / video router ───────────────────────────────────

async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route to logo-save or watermark-apply based on current user state."""
    awaiting = context.user_data.get("awaiting")

    if awaiting == "template_logo":
        # Only accept images (photo or image-type document), not videos
        if update.message.video:
            await update.message.reply_text("❌ 请发送图片文件，而不是视频。")
            return
        if update.message.document:
            fname = update.message.document.file_name or ""
            ext = pathlib.Path(fname).suffix.lstrip(".").lower()
            # Accept common image types; empty extension is ambiguous so we allow it
            if ext and ext not in {"png", "jpg", "jpeg", "webp", "gif"}:
                await update.message.reply_text("❌ 请发送图片文件（png/jpg/webp）作为水印 Logo。")
                return
        await _save_logo(update, context)
    else:
        await _apply_watermark(update, context)


# ── Application factory ───────────────────────────────────────────────────────

def build_application() -> Application:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN 环境变量未设置")

    db.init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("template", cmd_template))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(CommandHandler("addmember", cmd_addmember))
    app.add_handler(CommandHandler("revokemember", cmd_revokemember))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("webtoken", cmd_webtoken))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL,
            handle_media_message,
        )
    )
    return app


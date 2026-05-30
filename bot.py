"""Telegram 水印机器人。

发送图片或视频，机器人将自动添加水印后返回。
使用 /settings 修改水印参数。
"""

import asyncio
import logging
import os
import pathlib
import tempfile

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

POSITIONS = ["左上", "右上", "左下", "右下", "居中"]

DEFAULT_SETTINGS: dict = {
    "text": "© Wei",
    "position": "右下",
    "opacity": 75,
    "tiled": False,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_settings(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "settings" not in context.user_data:
        context.user_data["settings"] = DEFAULT_SETTINGS.copy()
    return context.user_data["settings"]


def settings_summary(s: dict) -> str:
    return (
        f"⚙️ 当前水印设置：\n"
        f"• 文字：{s['text']}\n"
        f"• 位置：{s['position']}\n"
        f"• 透明度：{s['opacity']}%\n"
        f"• 平铺：{'开' if s['tiled'] else '关'}"
    )


def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 修改水印文字", callback_data="set_text")],
        [InlineKeyboardButton(f"📍 位置: {s['position']}", callback_data="set_position")],
        [InlineKeyboardButton(f"🔆 透明度: {s['opacity']}%", callback_data="set_opacity")],
        [InlineKeyboardButton(f"🔲 平铺: {'开' if s['tiled'] else '关'}", callback_data="toggle_tiled")],
    ])


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 欢迎使用水印机器人！\n\n"
        "📷 直接发送图片或视频，我会加上水印并返回给你。\n"
        "📝 发送时可在说明文字里写水印内容（默认：© Wei）\n\n"
        "⚙️ 使用 /settings 查看和修改水印设置"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings(context)
    await update.message.reply_text(
        settings_summary(s),
        reply_markup=settings_keyboard(s),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    s = get_settings(context)
    data = query.data

    if data == "toggle_tiled":
        s["tiled"] = not s["tiled"]
        await query.edit_message_text(
            f"✅ 平铺水印已{'开启' if s['tiled'] else '关闭'}。\n\n"
            + settings_summary(s),
            reply_markup=settings_keyboard(s),
        )

    elif data == "set_position":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(p, callback_data=f"pos_{p}")] for p in POSITIONS]
        )
        await query.edit_message_text("请选择水印位置：", reply_markup=keyboard)

    elif data.startswith("pos_"):
        s["position"] = data[4:]
        await query.edit_message_text(
            f"✅ 位置已设为：{s['position']}\n\n" + settings_summary(s),
            reply_markup=settings_keyboard(s),
        )

    elif data == "set_opacity":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{v}%", callback_data=f"opacity_{v}") for v in [25, 50, 75, 100]]
        ])
        await query.edit_message_text("请选择透明度：", reply_markup=keyboard)

    elif data.startswith("opacity_"):
        s["opacity"] = int(data.split("_")[1])
        await query.edit_message_text(
            f"✅ 透明度已设为：{s['opacity']}%\n\n" + settings_summary(s),
            reply_markup=settings_keyboard(s),
        )

    elif data == "set_text":
        context.user_data["awaiting_text"] = True
        await query.edit_message_text("请发送新的水印文字：")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_text"):
        s = get_settings(context)
        s["text"] = update.message.text.strip() or s["text"]
        context.user_data["awaiting_text"] = False
        await update.message.reply_text(
            f"✅ 水印文字已设为：{s['text']}\n\n" + settings_summary(s),
            reply_markup=settings_keyboard(s),
        )
    else:
        await update.message.reply_text(
            "请直接发送图片或视频来添加水印，或使用 /settings 修改设置。"
        )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Lazy import to avoid circular dependency at module load time
    from main import add_watermark_to_image, add_watermark_to_video  # noqa: PLC0415

    s = get_settings(context)
    caption = (update.message.caption or "").strip()
    text = caption if caption else s["text"]

    msg = await update.message.reply_text("⏳ 正在处理，请稍候…")

    try:
        is_video = False
        ext = "jpg"

        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
        elif update.message.video:
            tg_file = await update.message.video.get_file()
            fname = update.message.video.file_name or "video.mp4"
            ext = pathlib.Path(fname).suffix.lstrip(".").lower() or "mp4"
            if ext not in {"mp4", "mov"}:
                await msg.edit_text("❌ 仅支持 mp4/mov 视频格式。")
                return
            is_video = True
        elif update.message.document:
            doc = update.message.document
            fname = doc.file_name or "file"
            ext = pathlib.Path(fname).suffix.lstrip(".").lower()
            if ext in {"mp4", "mov"}:
                is_video = True
            elif ext not in {"jpg", "jpeg", "png", "webp"}:
                await msg.edit_text("❌ 仅支持图片（jpg/png/webp）或视频（mp4/mov）。")
                return
            tg_file = await doc.get_file()
        else:
            await msg.edit_text("❌ 请发送图片或视频文件。")
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, f"input.{ext}")
            output_ext = "mp4" if is_video else "jpg"
            output_path = os.path.join(tmpdir, f"output.{output_ext}")

            await tg_file.download_to_drive(input_path)

            loop = asyncio.get_event_loop()
            if is_video:
                success = await loop.run_in_executor(
                    None,
                    add_watermark_to_video,
                    input_path, output_path, text,
                    s["position"], s["opacity"], s["tiled"], None,
                )
            else:
                success = await loop.run_in_executor(
                    None,
                    add_watermark_to_image,
                    input_path, output_path, text,
                    s["position"], s["opacity"], s["tiled"], None,
                )

            if not success:
                await msg.edit_text("❌ 处理失败，请重试。")
                return

            await msg.delete()
            caption_out = f"✅ 水印：{text}"
            if is_video:
                with open(output_path, "rb") as f:
                    await update.message.reply_video(f, caption=caption_out)
            else:
                with open(output_path, "rb") as f:
                    await update.message.reply_photo(f, caption=caption_out)

    except Exception as e:
        logger.error("处理媒体失败: %s", e, exc_info=True)
        await msg.edit_text(f"❌ 处理失败：{e}")


# ── Application factory ───────────────────────────────────────────────────────

def build_application() -> Application:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN 环境变量未设置")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL,
            handle_media,
        )
    )
    return app

import asyncio
import logging
import os
import pathlib
import re
import shutil
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial, wraps

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# MoviePy 1.x references PIL.Image.ANTIALIAS which was removed in Pillow 10.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS  # type: ignore[attr-defined]

from moviepy.editor import CompositeVideoClip, ImageClip, VideoFileClip
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

import db as _db

logger = logging.getLogger(__name__)

# ── Env config ────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    logger.error(
        "SECRET_KEY 环境变量未设置，已生成随机密钥——重启后所有 session 将失效。"
        "生产环境请设置固定的 SECRET_KEY。"
    )
WEB_ADMIN_PASSWORD = os.getenv("WEB_ADMIN_PASSWORD", "")
# Public HTTPS URL of this service, e.g. https://your-app.railway.app
# When set, the bot uses webhook mode instead of polling (avoids Conflict errors
# caused by multiple instances running simultaneously during rolling deploys).
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# Global reference to the running bot Application (set in lifespan)
_bot_app = None

# ── Upload size limit middleware (200 MB) ────────────────────────────────────
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB


class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_SIZE:
            return Response("请求体超过 200MB 限制", status_code=413)
        return await call_next(request)


# ── Lifespan: start Telegram bot if BOT_TOKEN is configured ─────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app
    if os.getenv("BOT_TOKEN"):
        try:
            from bot import build_application  # noqa: PLC0415
            bot_application = build_application()
            await bot_application.initialize()
            await bot_application.start()
            if WEBHOOK_URL:
                # Webhook mode: Telegram POSTs updates to our endpoint.
                # This avoids the "Conflict: terminated by other getUpdates
                # request" error that occurs when multiple instances (e.g.
                # during a rolling deploy) each try to poll simultaneously.
                token = os.getenv("BOT_TOKEN")
                await bot_application.bot.set_webhook(
                    url=f"{WEBHOOK_URL}/telegram-webhook/{token}"
                )
                logger.info("Telegram 机器人已启动（webhook 模式）")
            else:
                # Polling mode: suitable for local development only.
                await bot_application.updater.start_polling()
                logger.info("Telegram 机器人已启动（polling 模式）")
            _bot_app = bot_application
        except Exception as e:
            logger.error("Telegram 机器人启动失败: %s", e)
    yield
    if _bot_app is not None:
        try:
            if not WEBHOOK_URL:
                await _bot_app.updater.stop()
            # In webhook mode we intentionally skip delete_webhook() on
            # shutdown.  During a rolling deploy the new instance calls
            # set_webhook() first; if the old instance then calls
            # delete_webhook() it would silently remove the new instance's
            # webhook, leaving Telegram with nowhere to send updates.
            await _bot_app.stop()
            await _bot_app.shutdown()
            logger.info("Telegram 机器人已停止")
        except Exception as e:
            logger.error("Telegram 机器人停止失败: %s", e)
        finally:
            _bot_app = None


app = FastAPI(title="水印小程序", lifespan=lifespan)
app.add_middleware(LimitUploadSizeMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

for d in ["uploads", "outputs", "fonts", "logos", "user_logos"]:
    os.makedirs(d, exist_ok=True)

def _find_cjk_font() -> str:
    """Locate a CJK-capable TrueType font, searching local dir then system paths."""
    import glob as _glob
    # Preferred local copies (committed or placed at runtime)
    for name in ["wqy-zenhei.ttc", "simhei.ttf", "NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.otf"]:
        p = os.path.join("fonts", name)
        if os.path.exists(p):
            return p
    # Nix store (added via nixpacks wqy_zenhei package)
    for pat in _glob.glob("/nix/store/*/share/fonts/truetype/wqy-zenhei.ttc"):
        return pat
    # Common Linux system font paths
    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return os.path.join("fonts", "simhei.ttf")  # fallback (may not render CJK)


FONT_PATH = _find_cjk_font()

# Ensure DB is initialised on startup even when bot is not running
_db.init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_valid_telegram_id(value: str) -> bool:
    """Return True if *value* looks like a Telegram user ID.

    Telegram user IDs are positive integers; bot/group IDs may be negative.
    Accepting a leading '-' covers both cases.
    """
    return value.strip().lstrip("-").isdigit()


def _session_user(request: Request) -> dict | None:
    """Return the current session's user dict, or None if not logged in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    u = _db.get_user(int(user_id))
    if not u:
        return None
    u["role"] = _db.get_effective_role(u["user_id"], ADMIN_IDS)
    return u


def _require_login(func):
    """Decorator: redirect to /login if not authenticated."""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not _session_user(request):
            return RedirectResponse("/login", status_code=302)
        return await func(request, *args, **kwargs)
    return wrapper


def _require_admin(func):
    """Decorator: 403 if not an admin session."""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        u = _session_user(request)
        if not u or u["role"] != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return await func(request, *args, **kwargs)
    return wrapper


# ── Background cleanup ───────────────────────────────────────────────────────

def cleanup_file(path: str, delay: int = 300):
    """Sleep *delay* seconds, then delete *path* if it still exists."""
    time.sleep(delay)
    if os.path.exists(path):
        os.remove(path)


# ── Telegram webhook endpoint ─────────────────────────────────────────────────

@app.post("/telegram-webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Receive updates from Telegram in webhook mode.

    The bot token in the path acts as a shared secret so that only Telegram
    (which knows the token) can post updates here.
    """
    import secrets as _secrets  # noqa: PLC0415
    bot_token = os.getenv("BOT_TOKEN", "")
    # Capture local reference before any await so shutdown cannot null it out.
    bot_application = _bot_app
    if not _secrets.compare_digest(token, bot_token) or bot_application is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    from telegram import Update  # noqa: PLC0415
    try:
        data = await request.json()
        update = Update.de_json(data, bot_application.bot)
        # Enqueue the update for the PTB background dispatcher rather than
        # awaiting process_update() inline.  This returns 200 OK to Telegram
        # immediately so it does not retry the update after its ~30 s timeout
        # (which caused duplicate processing when video encoding took too long).
        await bot_application.update_queue.put(update)
    except Exception as exc:
        logger.error("处理 Telegram webhook 更新时出错: %s", exc)
    return {"ok": True}


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _session_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    user_id: str = Form(...),
    credential: str = Form(...),
):
    if not _is_valid_telegram_id(user_id):
        return templates.TemplateResponse(request, "login.html", {"error": "Telegram ID 格式错误"})

    uid = int(user_id.strip())

    # Admin login via password
    if uid in ADMIN_IDS:
        if WEB_ADMIN_PASSWORD and credential.strip() == WEB_ADMIN_PASSWORD:
            _db.ensure_user(uid)
            request.session["user_id"] = uid
            return RedirectResponse("/admin", status_code=302)
        return templates.TemplateResponse(request, "login.html", {"error": "管理员密码错误"})

    # Regular / member login via web token
    u = _db.get_user(uid)
    if not u or not u.get("web_token") or u["web_token"] != credential.strip():
        return templates.TemplateResponse(request, "login.html", {"error": "令牌无效，请在 Telegram 机器人发送 /webtoken 获取"})

    request.session["user_id"] = uid
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/autologin")
async def autologin(request: Request, token: str = ""):
    """One-click login via magic link sent by the Telegram bot.

    The bot embeds the user's web_token in the URL so the user never has to
    copy/paste anything.  Role (admin / member / regular) is detected
    automatically from the database and ADMIN_IDS.
    """
    # Validate token format: token_urlsafe(24) produces 32 URL-safe base64 chars
    _TOKEN_RE = re.compile(r'^[A-Za-z0-9\-_]{20,64}$')
    if not token or not _TOKEN_RE.match(token.strip()):
        return RedirectResponse("/login", status_code=302)

    u = _db.get_user_by_token(token.strip())
    if not u:
        return templates.TemplateResponse(
            request, "login.html", {"error": "链接已失效，请在 Telegram 机器人重新发送 /webtoken 获取新链接"}
        )

    # Prevent session fixation: clear any existing session before setting new identity
    request.session.clear()
    request.session["user_id"] = u["user_id"]
    role = _db.get_effective_role(u["user_id"], ADMIN_IDS)
    if role == "admin":
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


# ── Home redirect ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    u = _session_user(request)
    if not u:
        return RedirectResponse("/login", status_code=302)
    if u["role"] == "admin":
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


# ── User dashboard ────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
@_require_login
async def dashboard(request: Request):
    u = _session_user(request)
    s = _db.get_watermark_settings(u["user_id"])
    used, limit = _db.get_daily_usage(u["user_id"])
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": u,
        "settings": s,
        "used": used,
        "limit": limit,
    })


@app.post("/save_settings")
@_require_login
async def save_settings(
    request: Request,
    wm_type: str = Form("text"),
    text: str = Form("© Wei"),
    position: str = Form("右下"),
    opacity: int = Form(75),
    tiled: str = Form("false"),
    font_size: int = Form(5),
    logo_scale: int = Form(20),
    logo: UploadFile = File(None),
):
    u = _session_user(request)
    uid = u["user_id"]
    tiled_bool = tiled.lower() in ("true", "on", "1")

    logo_path = None
    if logo and logo.filename:
        safe_logo = pathlib.Path(logo.filename).name
        ext = safe_logo.rsplit(".", 1)[-1].lower() if "." in safe_logo else "png"
        logo_path = f"user_logos/{uid}.{ext}"
        os.makedirs("user_logos", exist_ok=True)
        with open(logo_path, "wb") as f:
            shutil.copyfileobj(logo.file, f)

    watermark_settings: dict = {
        "wm_type": wm_type,
        "text": text,
        "position": position,
        "opacity": opacity,
        "tiled": int(tiled_bool),
        "font_size": max(1, min(15, font_size)),
        "logo_scale": max(5, min(100, logo_scale)),
    }
    if logo_path:
        watermark_settings["logo_path"] = logo_path
    _db.save_watermark_settings(uid, **watermark_settings)
    return RedirectResponse("/dashboard?saved=1", status_code=302)


# ── Watermark endpoint (session-aware) ────────────────────────────────────────


@app.post("/add_watermark")
async def add_watermark(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    text: str = Form("© Wei"),
    position: str = Form("右下"),
    opacity: int = Form(75),
    tiled: str = Form("false"),          # Fix #2: receive as string
    font_size: int = Form(5),
    logo_scale: int = Form(20),
    logo: UploadFile = File(None),
    pos_x: float | None = Form(None),  # watermark centre X as % of image width (0–100)
    pos_y: float | None = Form(None),  # watermark centre Y as % of image height (0–100)
):
    tiled_bool = tiled.lower() in ("true", "on", "1")  # Fix #2: parse manually
    font_size = max(1, min(15, font_size))
    logo_scale = max(5, min(100, logo_scale))

    # Session-based quota check for regular users
    u = _session_user(request)
    if u and u["role"] == "regular":
        allowed, _ = _db.check_and_increment_usage(u["user_id"])
        if not allowed:
            raise HTTPException(status_code=429, detail="今日使用次数已达上限")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    # Sanitize filenames to prevent path-traversal in upload directories
    safe_filename = pathlib.Path(file.filename).name
    ext = safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else ""

    allowed_exts = {"jpg", "jpeg", "png", "webp", "mp4", "mov"}
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="不支持的文件类型")

    input_path = f"uploads/{timestamp}_{safe_filename}"

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logo_path = None
    if logo and logo.filename:
        safe_logo_filename = pathlib.Path(logo.filename).name
        logo_path = f"logos/{timestamp}_{safe_logo_filename}"
        with open(logo_path, "wb") as buffer:
            shutil.copyfileobj(logo.file, buffer)

    output_path = f"outputs/watermarked_{timestamp}.{ext}"
    # Images are always re-saved as JPEG (RGB); use .jpg extension for consistency
    if ext in ["jpg", "jpeg", "png", "webp"]:
        output_path = f"outputs/watermarked_{timestamp}.jpg"

    success = False
    if ext in ["jpg", "jpeg", "png", "webp"]:
        success = add_watermark_to_image(
            input_path, output_path, text, position, opacity, tiled_bool, logo_path,
            pos_x=pos_x, pos_y=pos_y, font_size=font_size, logo_scale=logo_scale,
        )
    elif ext in ["mp4", "mov"]:
        # Fix #7: run blocking video work in a thread-pool executor
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            partial(add_watermark_to_video,
                    input_path, output_path, text, position, opacity, tiled_bool, logo_path, pos_x, pos_y, font_size, logo_scale),
        )

    # Clean up uploaded originals immediately
    if os.path.exists(input_path):
        os.remove(input_path)
    if logo_path and os.path.exists(logo_path):
        os.remove(logo_path)

    if success:
        # Fix #4: schedule output file deletion after 300 s
        background_tasks.add_task(cleanup_file, output_path, 300)
        return {
            "success": True,
            "download_url": f"/download/{os.path.basename(output_path)}",
            "filename": os.path.basename(output_path),
        }
    return {"success": False, "error": "处理失败"}


@app.get("/download/{filename}")
async def download(filename: str):
    # Fix #1: strict whitelist — reconstruct filename from captured regex groups so
    # CodeQL / taint analysis sees a freshly-built string, not raw user input.
    m = re.match(r'^([\w\-]+)(\.\w+)?$', filename)
    if not m:
        raise HTTPException(status_code=400, detail="非法文件名")
    # Reconstruct from matched groups (no directory separators possible)
    clean_filename = m.group(1) + (m.group(2) or "")
    outputs_dir = pathlib.Path("outputs").resolve()
    safe_path = outputs_dir / clean_filename
    # Verify the path stays inside outputs/ (handles any edge-case symlinks)
    try:
        safe_path.resolve().relative_to(outputs_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法文件名")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(safe_path, filename=clean_filename)


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
@_require_admin
async def admin_home(request: Request):
    stats = _db.get_stats()
    sys_settings = _db.get_system_settings()
    return templates.TemplateResponse(request, "admin.html", {
        "user": _session_user(request),
        "stats": stats,
        "sys_settings": sys_settings,
        "tab": "overview",
    })


@app.get("/admin/users", response_class=HTMLResponse)
@_require_admin
async def admin_users(request: Request, page: int = 1, search: str = ""):
    flash = request.session.pop("flash", None)
    users, total = _db.list_users(page=page, limit=20, search=search)
    today = datetime.now().date().isoformat()
    # Annotate effective role
    for u in users:
        u["effective_role"] = _db.get_effective_role(u["user_id"], ADMIN_IDS)
        u["member_expired"] = (
            u["role"] == "member"
            and u.get("member_until")
            and u["member_until"] < today
        )
    pages = max(1, (total + 19) // 20)
    return templates.TemplateResponse(request, "admin.html", {
        "user": _session_user(request),
        "users": users,
        "total": total,
        "page": page,
        "pages": pages,
        "search": search,
        "flash": flash,
        "tab": "users",
    })


@app.post("/admin/members/add")
@_require_admin
async def admin_add_member(
    request: Request,
    target_id: int = Form(...),
    days: int = Form(...),
):
    _db.ensure_user(target_id)
    until = _db.add_member(target_id, days)
    request.session["flash"] = f"已为用户 {target_id} 授权 {days} 天会员，到期：{until}"
    return RedirectResponse("/admin/users", status_code=302)


@app.post("/admin/members/revoke")
@_require_admin
async def admin_revoke_member(request: Request, target_id: int = Form(...)):
    _db.revoke_member(target_id)
    request.session["flash"] = f"已撤销用户 {target_id} 的会员资格"
    return RedirectResponse("/admin/users", status_code=302)


@app.get("/admin/settings", response_class=HTMLResponse)
@_require_admin
async def admin_settings_page(request: Request):
    sys_settings = _db.get_system_settings()
    return templates.TemplateResponse(request, "admin.html", {
        "user": _session_user(request),
        "sys_settings": sys_settings,
        "tab": "settings",
    })


@app.post("/admin/settings")
@_require_admin
async def admin_settings_save(
    request: Request,
    default_text: str = Form("© Wei"),
    default_position: str = Form("右下"),
    default_opacity: int = Form(75),
    default_tiled: str = Form("0"),
    daily_limit: int = Form(3),
):
    _db.save_system_settings(
        default_text=default_text,
        default_position=default_position,
        default_opacity=default_opacity,
        default_tiled="1" if default_tiled in ("1", "true", "on") else "0",
        daily_limit=daily_limit,
    )
    return RedirectResponse("/admin/settings?saved=1", status_code=302)


# ── Image watermark ──────────────────────────────────────────────────────────

def add_watermark_to_image(input_path, output_path, text, position, opacity, tiled, logo_path=None, pos_x=None, pos_y=None, font_size=5, logo_scale=20):
    try:
        img = Image.open(input_path).convert("RGBA")
        w, h = img.size

        if logo_path:
            logo = Image.open(logo_path).convert("RGBA")
            logo_size = int(min(w, h) * max(5, min(100, logo_scale)) / 100)
            lw, lh = logo.size
            if lw <= 0 or lh <= 0:
                lw, lh = max(1, lw), max(1, lh)
            if lw >= lh:
                new_w = logo_size
                new_h = max(1, int(lh * logo_size / lw))
            else:
                new_h = logo_size
                new_w = max(1, int(lw * logo_size / lh))
            logo = logo.resize((new_w, new_h), Image.Resampling.LANCZOS)
            logo = ImageEnhance.Brightness(logo).enhance(opacity / 100)

            if tiled:
                step = int(logo_size * 1.7)
                for x in range(0, w, step):
                    for y in range(0, h, step):
                        img.paste(logo, (x, y), logo)
            else:
                pos = get_position(position, w, h, new_w, new_h, pos_x=pos_x, pos_y=pos_y)
                img.paste(logo, pos, logo)
        else:
            px_size = max(12, int(h * font_size / 100))
            try:
                font = ImageFont.truetype(FONT_PATH, px_size)
            except (IOError, OSError, ValueError, RuntimeError) as e:  # Fix #6
                print(f"字体加载失败，使用默认字体: {e}")
                font = ImageFont.load_default()

            if tiled:
                layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(layer)
                alpha = int(255 * opacity / 100)
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                spacing_x = int(tw * 1.8)
                spacing_y = int(th * 2.0)
                for x in range(-tw, w, spacing_x):
                    for y in range(-th, h, spacing_y):
                        draw.text((x, y), text, fill=(255, 255, 255, alpha), font=font)
                img = Image.alpha_composite(img, layer)
            else:
                draw = ImageDraw.Draw(img)
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pos = get_position(position, w, h, tw, th, pos_x=pos_x, pos_y=pos_y)
                alpha = int(255 * opacity / 100)
                draw.text((pos[0] + 2, pos[1] + 2), text, fill=(0, 0, 0, alpha), font=font)
                draw.text(pos, text, fill=(255, 255, 255, alpha), font=font)

        img.convert("RGB").save(output_path, quality=95)
        return True
    except Exception as e:
        print("图片处理错误:", e)
        return False


# ── Video watermark ──────────────────────────────────────────────────────────

def _make_positioned_watermark_image(text, video_w, video_h, opacity, font_path, position, pos_x=None, pos_y=None, font_size=5):
    """Build a full-frame RGBA PIL image with text at the specified position."""
    px_size = max(12, int(video_h * font_size / 100))
    try:
        font = ImageFont.truetype(font_path, px_size)
    except (IOError, OSError, ValueError, RuntimeError) as e:
        print(f"视频水印字体加载失败，使用默认字体: {e}")
        font = ImageFont.load_default()

    layer = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    alpha = int(255 * opacity / 100)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pos = get_position(position, video_w, video_h, tw, th, pos_x=pos_x, pos_y=pos_y)
    draw.text((pos[0] + 2, pos[1] + 2), text, fill=(0, 0, 0, alpha), font=font)
    draw.text(pos, text, fill=(255, 255, 255, alpha), font=font)
    return layer


def _make_tiled_watermark_image(text, video_w, video_h, opacity, font_path, font_size=5):
    """Build a full-frame RGBA PIL image with tiled text, same logic as image path."""
    px_size = max(12, int(video_h * font_size / 100))
    try:
        font = ImageFont.truetype(font_path, px_size)
    except (IOError, OSError, ValueError, RuntimeError) as e:
        print(f"视频水印字体加载失败，使用默认字体: {e}")
        font = ImageFont.load_default()

    layer = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    alpha = int(255 * opacity / 100)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    spacing_x = int(tw * 1.8)
    spacing_y = int(th * 2.0)
    for x in range(-tw, video_w, spacing_x):
        for y in range(-th, video_h, spacing_y):
            draw.text((x, y), text, fill=(255, 255, 255, alpha), font=font)
    return layer


def add_watermark_to_video(input_path, output_path, text, position, opacity, tiled, logo_path=None, pos_x=None, pos_y=None, font_size=5, logo_scale=20):
    try:
        clip = VideoFileClip(input_path)

        if logo_path:
            logo_h = int(min(clip.w, clip.h) * max(5, min(100, logo_scale)) / 100)
            logo_clip = ImageClip(logo_path).resize(height=logo_h)
            logo_clip = logo_clip.set_duration(clip.duration).set_opacity(opacity / 100)
            pos = get_position(position, clip.w, clip.h, logo_clip.w, logo_clip.h, pos_x=pos_x, pos_y=pos_y)
            logo_clip = logo_clip.set_position(pos)
            final = CompositeVideoClip([clip, logo_clip])
        else:
            if tiled:
                # Fix #3: PIL-generated tiled overlay, consistent with image path
                overlay_img = _make_tiled_watermark_image(
                    text, clip.w, clip.h, opacity, FONT_PATH, font_size=font_size
                )
                # Use a cross-platform temp file; clean it up after writing the video
                tmp_fd, tmp_overlay = tempfile.mkstemp(suffix=".png")
                os.close(tmp_fd)
                try:
                    overlay_img.save(tmp_overlay)
                    overlay_clip = (
                        ImageClip(tmp_overlay)
                        .set_duration(clip.duration)
                    )
                    final = CompositeVideoClip([clip, overlay_clip])
                    # Fix #5: use cpu_count() for encoding threads
                    final.write_videofile(
                        output_path,
                        codec="libx264",
                        audio_codec="aac",
                        threads=os.cpu_count() or 4,
                        preset="medium",
                        verbose=False,
                        logger=None,
                    )
                finally:
                    if os.path.exists(tmp_overlay):
                        os.remove(tmp_overlay)
                return True
            else:
                overlay_img = _make_positioned_watermark_image(
                    text, clip.w, clip.h, opacity, FONT_PATH, position, pos_x=pos_x, pos_y=pos_y, font_size=font_size
                )
                tmp_fd, tmp_overlay = tempfile.mkstemp(suffix=".png")
                os.close(tmp_fd)
                try:
                    overlay_img.save(tmp_overlay)
                    overlay_clip = (
                        ImageClip(tmp_overlay)
                        .set_duration(clip.duration)
                    )
                    final = CompositeVideoClip([clip, overlay_clip])
                    final.write_videofile(
                        output_path,
                        codec="libx264",
                        audio_codec="aac",
                        threads=os.cpu_count() or 4,
                        preset="medium",
                        verbose=False,
                        logger=None,
                    )
                finally:
                    if os.path.exists(tmp_overlay):
                        os.remove(tmp_overlay)
                return True

        # Fix #5: use cpu_count() for encoding threads
        final.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            threads=os.cpu_count() or 4,  # Fix #5
            preset="medium",
            verbose=False,
            logger=None,
        )
        return True
    except Exception as e:
        print("视频处理错误:", e)
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_position(pos_type, w, h, item_w, item_h, pos_x=None, pos_y=None):
    if pos_x is not None and pos_y is not None:
        x = int(w * pos_x / 100) - item_w // 2
        y = int(h * pos_y / 100) - item_h // 2
        return (max(0, min(w - item_w, x)), max(0, min(h - item_h, y)))
    m = max(15, int(min(w, h) * 0.03))
    if pos_type == "左上":
        return (m, m)
    if pos_type == "右上":
        return (w - item_w - m, m)
    if pos_type == "中上":
        return ((w - item_w) // 2, m)
    if pos_type == "左下":
        return (m, h - item_h - m)
    if pos_type == "右下":
        return (w - item_w - m, h - item_h - m)
    if pos_type == "中下":
        return ((w - item_w) // 2, h - item_h - m)
    if pos_type == "居中":
        return ((w - item_w) // 2, (h - item_h) // 2)
    return (w - item_w - m, h - item_h - m)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

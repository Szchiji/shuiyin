import asyncio
import os
import pathlib
import re
import shutil
import tempfile
import time
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from moviepy.editor import CompositeVideoClip, ImageClip, VideoFileClip
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

# ── Upload size limit middleware (200 MB) ────────────────────────────────────
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB


class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_SIZE:
            return Response("请求体超过 200MB 限制", status_code=413)
        return await call_next(request)


app = FastAPI(title="水印小程序")
app.add_middleware(LimitUploadSizeMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

for d in ["uploads", "outputs", "fonts", "logos"]:
    os.makedirs(d, exist_ok=True)

FONT_PATH = "fonts/simhei.ttf"


# ── Background cleanup ───────────────────────────────────────────────────────

def cleanup_file(path: str, delay: int = 300):
    """Sleep *delay* seconds, then delete *path* if it still exists."""
    time.sleep(delay)
    if os.path.exists(path):
        os.remove(path)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/add_watermark")
async def add_watermark(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    text: str = Form("© Wei"),
    position: str = Form("右下"),
    opacity: int = Form(75),
    tiled: str = Form("false"),          # Fix #2: receive as string
    logo: UploadFile = File(None),
):
    tiled_bool = tiled.lower() in ("true", "on", "1")  # Fix #2: parse manually

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
            input_path, output_path, text, position, opacity, tiled_bool, logo_path
        )
    elif ext in ["mp4", "mov"]:
        # Fix #7: run blocking video work in a thread-pool executor
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            add_watermark_to_video,
            input_path, output_path, text, position, opacity, tiled_bool, logo_path,
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


# ── Image watermark ──────────────────────────────────────────────────────────

def add_watermark_to_image(input_path, output_path, text, position, opacity, tiled, logo_path=None):
    try:
        img = Image.open(input_path).convert("RGBA")
        w, h = img.size

        if logo_path:
            logo = Image.open(logo_path).convert("RGBA")
            logo_size = int(min(w, h) * 0.18)
            logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
            logo = ImageEnhance.Brightness(logo).enhance(opacity / 100)

            if tiled:
                step = int(logo_size * 1.7)
                for x in range(0, w, step):
                    for y in range(0, h, step):
                        img.paste(logo, (x, y), logo)
            else:
                pos = get_position(position, w, h, logo_size, logo_size)
                img.paste(logo, pos, logo)
        else:
            try:
                font = ImageFont.truetype(FONT_PATH, int(h / 22))
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
                pos = get_position(position, w, h, tw, th)
                alpha = int(255 * opacity / 100)
                draw.text((pos[0] + 2, pos[1] + 2), text, fill=(0, 0, 0, alpha), font=font)
                draw.text(pos, text, fill=(255, 255, 255, alpha), font=font)

        img.convert("RGB").save(output_path, quality=95)
        return True
    except Exception as e:
        print("图片处理错误:", e)
        return False


# ── Video watermark ──────────────────────────────────────────────────────────

def _make_tiled_watermark_image(text, video_w, video_h, opacity, font_path):
    """Build a full-frame RGBA PIL image with tiled text, same logic as image path."""
    try:
        font = ImageFont.truetype(font_path, int(video_h / 22))
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


def add_watermark_to_video(input_path, output_path, text, position, opacity, tiled, logo_path=None):
    try:
        clip = VideoFileClip(input_path)

        if logo_path:
            logo_clip = ImageClip(logo_path).resize(height=clip.h // 8)
            logo_clip = logo_clip.set_duration(clip.duration).set_opacity(opacity / 100)
            pos = get_position(position, clip.w, clip.h, logo_clip.w, logo_clip.h)
            logo_clip = logo_clip.set_position(pos)
            final = CompositeVideoClip([clip, logo_clip])
        else:
            if tiled:
                # Fix #3: PIL-generated tiled overlay, consistent with image path
                overlay_img = _make_tiled_watermark_image(
                    text, clip.w, clip.h, opacity, FONT_PATH
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
                from moviepy.editor import TextClip
                try:
                    font_arg = "simhei"
                    txt_clip = TextClip(
                        text, fontsize=clip.h // 25, color="white",
                        font=font_arg, stroke_color="black", stroke_width=2,
                    )
                except Exception as e:
                    print(f"TextClip 字体 '{font_arg}' 加载失败，使用默认字体: {e}")
                    txt_clip = TextClip(
                        text, fontsize=clip.h // 25, color="white",
                        stroke_color="black", stroke_width=2,
                    )
                txt_clip = txt_clip.set_opacity(opacity / 100).set_duration(clip.duration)
                pos_map = {
                    "左上": ("left", "top"),
                    "右上": ("right", "top"),
                    "左下": ("left", "bottom"),
                    "右下": ("right", "bottom"),
                    "居中": ("center", "center"),
                }
                txt_clip = txt_clip.set_position(pos_map.get(position, ("right", "bottom")))
                final = CompositeVideoClip([clip, txt_clip])

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

def get_position(pos_type, w, h, item_w, item_h):
    m = 40
    if pos_type == "左上":
        return (m, m)
    if pos_type == "右上":
        return (w - item_w - m, m)
    if pos_type == "左下":
        return (m, h - item_h - m)
    if pos_type == "右下":
        return (w - item_w - m, h - item_h - m)
    if pos_type == "居中":
        return ((w - item_w) // 2, (h - item_h) // 2)
    return (w - item_w - m, h - item_h - m)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

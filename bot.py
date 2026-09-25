import os
import re
import html
import asyncio
import random
import threading
import time
import uuid
import shutil
from collections import deque, OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN,
    REQUEST_TIMEOUT,
    PORT,
    ADMIN_USER_ID,
    MAX_IMAGE_FILE_SIZE,
    MAX_IMAGE_PIXELS,
    MAX_PHOTO_FILE_SIZE,
    MAX_FILE_SIZE,
    MAX_AUDIO_FILE_SIZE,
    MAX_CONCURRENT_JOBS,
    MAX_PHOTO_COUNT,
    MAX_TEXT_LENGTH,
    URL_VALIDATION_TIMEOUT,
    USER_RATE_LIMIT_SECONDS,
    logger,
    DOWNLOAD_DIR,
)
from downloader import (
    extract_and_download,
    validate_media_url,
    cleanup_stale_jobs,
    MediaFileTooLargeError,
    VideoTooLargeError,
    VideoDownloadError,
    YOUTUBE_PLAYER_CLIENTS,
)
from security import InvalidURL, redact_url
from image_processor import enhance_image, beautify_image
from progress import TelegramProgress
from tools import (
    generate_qr,
    make_sticker,
    video_to_gif,
    add_meme_text,
    compress_image,
    extract_colors,
    add_watermark,
    get_youtube_thumbnail,
    parse_dice,
    image_to_ascii,
    extract_youtube_id,
)

_activity_log: deque[dict] = deque(maxlen=200)
_download_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_request_times: OrderedDict[int, float] = OrderedDict()
_bot_ready = threading.Event()
_bot_shutdown = threading.Event()


def _log_activity(
    user_id: int,
    username: str,
    first_name: str,
    url: str,
    platform: str,
    status: str,
) -> None:
    _activity_log.append({
        "time": time.time(),
        "user_id": user_id,
        "username": username or "-",
        "name": first_name or "-",
        "url": redact_url(url)[:160],
        "platform": platform,
        "status": status,
    })


def _cleanup_path(path: str | Path | None) -> None:
    if not path:
        return
    target = Path(path)
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    except OSError:
        pass


def _check_output_file(path: str, max_bytes: int, label: str) -> None:
    size = os.path.getsize(path) if os.path.isfile(path) else max_bytes + 1
    if size > max_bytes:
        raise MediaFileTooLargeError(size, max_bytes, label)


def _format_media_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(0, size) / 1024:.0f} KB"


def _format_media_duration(seconds: int) -> str:
    total = max(0, int(seconds or 0))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _request_allowed(user_id: int) -> bool:
    if USER_RATE_LIMIT_SECONDS <= 0:
        return True
    now = time.monotonic()
    while _request_times and now - next(iter(_request_times.values())) > USER_RATE_LIMIT_SECONDS * 4:
        _request_times.popitem(last=False)
    previous = _request_times.get(user_id)
    if previous is not None and now - previous < USER_RATE_LIMIT_SECONDS:
        return False
    _request_times[user_id] = now
    _request_times.move_to_end(user_id)
    while len(_request_times) > 10_000:
        _request_times.popitem(last=False)
    return True


URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)
BARE_URL_REGEX = re.compile(
    r"(?:www\.)?(?:tiktok\.com|douyin\.com|iesdouyin\.com|youtube\.com|youtu\.be|facebook\.com|fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)


def _extract_url_from_message(message) -> str:
    text = message.text or message.caption or ""
    match = URL_REGEX.search(text)
    if match:
        return match.group(0).rstrip(".,!?;:]})\"")
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
    for entity in entities:
        if entity.type == "text_link" and entity.url:
            return str(entity.url)
        if entity.type == "url":
            candidate = text[entity.offset:entity.offset + entity.length]
            if candidate:
                return candidate.rstrip(".,!?;:]})\"")
    bare = BARE_URL_REGEX.search(text)
    if bare:
        candidate = bare.group(0).rstrip(".,!?;:]})\"")
        return f"https://{candidate}"
    return ""


async def _safe_reply(message, text: str, parse_mode=None) -> bool:
    if not message:
        return False
    try:
        await asyncio.wait_for(
            message.reply_text(text, parse_mode=parse_mode),
            timeout=10,
        )
        return True
    except Exception as exc:
        logger.warning("Không gửi được phản hồi Telegram: %s", exc)
        return False


async def _report_progress_error(progress, message, text: str) -> bool:
    if progress and await progress.fail(text):
        return True
    return await _safe_reply(message, text, ParseMode.HTML)


def _safe_error_detail(error: BaseException | str, max_length: int = 2500) -> str:
    text = str(error)
    text = re.sub(r"https?://[^\s<>]+", lambda match: redact_url(match.group(0), 180), text)
    text = re.sub(
        r"(?i)(cookie(?:s)?\s*[:=]\s*)[^\n]+",
        r"\1[redacted]",
        text,
    )
    return text[:max_length]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lệnh /start - Chào mừng và hướng dẫn người dùng."""
    user = update.effective_user
    greeting = (
        f"👋 Xin chào <b>{html.escape(user.first_name if user else 'Bạn')}</b>!\n\n"
        "🤖 Tôi là <b>Media Downloader Bot</b> chuyên:\n"
        "• 🎵 <b>TikTok</b> (Video và album ảnh)\n"
        "• 📘 <b>Facebook</b> (Chất lượng HD cao nhất)\n"
        "• 📺 <b>YouTube</b> (Video kèm âm thanh đầy đủ)\n\n"
        "🖼️ <b>Xử lý ảnh:</b>\n"
        "• 🔍 /enhance — Làm nét ảnh (sharpen + tăng màu)\n"
        "• ✨ /beautify — Làm đẹp ảnh (mịn da + sáng + hồng)\n\n"
        "🛠️ <b>10 Tools hay:</b>\n"
        "• 📱 /qr — Tạo QR code\n"
        "• 🏷️ /sticker — Ảnh → sticker Telegram\n"
        "• 🎞️ /gif — Video → GIF\n"
        "• 😂 /meme — Thêm text meme lên ảnh\n"
        "• 📦 /compress — Nén ảnh giảm dung lượng\n"
        "• 🎨 /colors — Trích xuất bảng màu\n"
        "• 💧 /watermark — Thêm watermark lên ảnh\n"
        "• 🖼️ /thumb — Tải thumbnail YouTube\n"
        "• 🎲 /roll — Random dice\n"
        "• ⌨️ /ascii — Ảnh → ASCII art\n\n"
        "📌 <b>Cách sử dụng:</b>\n"
        "• Gửi link video → tải tự động\n"
        "• Hoặc dùng <code>/download &lt;link&gt;</code>\n"
        "• Gõ lệnh tool → gửi ảnh → nhận kết quả\n\n"
        "⚠️ <i>Lưu ý: Telegram Bot giới hạn file tối đa <b>50MB</b>.</i>"
    )
    if update.message:
        await update.message.reply_text(greeting, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lệnh /help - Cung cấp thông tin trợ giúp chi tiết."""
    help_text = (
        "📖 <b>HƯỚNG DẪN SỬ DỤNG VÀ LƯU Ý</b>\n\n"
        "1️⃣ <b>Gửi link:</b> Gửi một tin nhắn chứa liên kết video từ TikTok, Facebook, YouTube hoặc các nền tảng được hỗ trợ.\n"
        "2️⃣ <b>Xử lý tự động:</b> Bot sẽ trích xuất luồng video phù hợp, dùng FFmpeg gộp âm thanh và chuẩn hóa codec.\n"
        "3️⃣ <b>Giới hạn:</b> Do chính sách của Telegram Bot API, các video có dung lượng trên <b>50MB</b> sẽ không thể gửi trực tiếp qua bot.\n"
        "4️⃣ <b>Quyền riêng tư:</b> Bot chỉ tải các video ở chế độ công khai (Public)."
    )
    if update.message:
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xem lịch sử gửi link của users (chỉ admin)."""
    if not update.message:
        return

    user = update.effective_user
    if ADMIN_USER_ID == 0 or user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Bạn không có quyền truy cập lệnh này.")
        return

    if not _activity_log:
        await update.message.reply_text("📋 Chưa có hoạt động nào được ghi nhận.")
        return

    # Đếm user unique + tổng request
    users = {}
    for entry in _activity_log:
        uid = entry["user_id"]
        if uid not in users:
            users[uid] = {"name": entry["name"], "username": entry["username"], "count": 0, "last": entry}
        users[uid]["count"] += 1
        users[uid]["last"] = entry

    # Tổng quan
    total = len(_activity_log)
    success = sum(1 for e in _activity_log if e["status"] == "✅")
    failed = total - success
    lines = [
        "📊 <b>ADMIN DASHBOARD</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📥 Tổng requests: <b>{total}</b> | ✅ {success} | ❌ {failed}",
        f"👤 Users: <b>{len(users)}</b>",
        "",
        "👤 <b>DANH SÁCH USERS</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for uid, info in sorted(users.items(), key=lambda x: -x[1]["count"]):
        uname = f"@{info['username']}" if info["username"] != "-" else f"ID: {uid}"
        lines.append(
            f"• {html.escape(str(info['name']))} ({html.escape(uname)}) — "
            f"<b>{info['count']}</b> link"
        )

    # 10 hoạt động gần nhất
    lines.append("")
    lines.append("📜 <b>10 HOẠT ĐỘNG GẦN NHẤT</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    for entry in list(_activity_log)[-10:][::-1]:
        t = time.strftime("%d/%m %H:%M", time.localtime(entry["time"]))
        uname = f"@{entry['username']}" if entry["username"] != "-" else f"ID:{entry['user_id']}"
        url_short = entry["url"][:50] + ("..." if len(entry["url"]) > 50 else "")
        lines.append(
            f"{entry['status']} <code>{t}</code> "
            f"{html.escape(str(entry['name']))} ({html.escape(uname)})\n"
            f"  🌐 {html.escape(str(entry['platform']))} — "
            f"<code>{html.escape(url_short)}</code>"
        )

    text = "\n".join(lines)

    # Telegram limit 4096 chars
    if len(text) > 4000:
        text = text[:3990] + "\n..."

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════════════════════
#  IMAGE PROCESSING — /enhance & /beautify
# ═══════════════════════════════════════════════════════════════════════════════

async def enhance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kích hoạt chế độ làm nét — user gửi ảnh tiếp theo sẽ được xử lý."""
    if not update.message:
        return
    context.user_data.update(mode="enhance", mode_at=time.monotonic())
    await update.message.reply_text(
        "🔍 <b>CHẾ ĐỘ LÀM NÉT ẢNH</b>\n\n"
        "📸 Gửi ảnh cần làm nét!\n"
        "⏳ Ảnh sẽ được sharpen + tăng màu sắc tự động.\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def beautify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kích hoạt chế độ làm đẹp — user gửi ảnh tiếp theo sẽ được xử lý."""
    if not update.message:
        return
    context.user_data.update(mode="beautify", mode_at=time.monotonic())
    await update.message.reply_text(
        "✨ <b>CHẾ ĐỘ LÀM ĐẸP ẢNH</b>\n\n"
        "📸 Gửi ảnh cần làm đẹp!\n"
        "⏳ Ảnh sẽ được mịn da + sáng + tông hồng ấm tự động.\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hủy chế độ xử lý ảnh."""
    if not update.message:
        return
    if context.user_data.get("mode"):
        context.user_data.pop("mode", None)
        context.user_data.pop("mode_at", None)
        context.user_data.pop("args", None)
        await update.message.reply_text("✅ Đã hủy chế độ hiện tại.")
    else:
        await update.message.reply_text("Không có chế độ nào đang hoạt động.")


# ═══════════════════════════════════════════════════════════════════════════════
#  10 TOOLS — Command handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def qr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 1: /qr <text> — tạo QR code PNG."""
    if not update.message:
        return
    args = context.args if context.args else []
    if not args:
        await update.message.reply_text(
            "📱 <b>QR CODE</b>\n\n"
            "Cú pháp: <code>/qr &lt;nội dung&gt;</code>\n\n"
            "VD: <code>/qr https://github.com</code>\n"
            "VD: <code>/qr Hello World</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(args)
    if len(text) > min(MAX_TEXT_LENGTH, 2000):
        await update.message.reply_text("❌ Nội dung QR quá dài.")
        return
    output_path = str(DOWNLOAD_DIR / f"qr_{uuid.uuid4().hex[:8]}.png")
    try:
        await asyncio.to_thread(generate_qr, text, output_path)
        with open(output_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"📱 QR code cho: <code>{html.escape(text[:100])}</code>",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f"QR lỗi: {e}", exc_info=True)
        await update.message.reply_text("❌ Không thể tạo QR code. Vui lòng thử lại!")
    finally:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass


async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 2: /sticker — gửi ảnh → nhận sticker 512x512."""
    if not update.message:
        return
    context.user_data.update(mode="sticker", mode_at=time.monotonic())
    await update.message.reply_text(
        "🏷️ <b>STICKER MAKER</b>\n\n"
        "📸 Gửi ảnh để chuyển thành sticker 512x512!\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def gif_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 3: /gif — reply video với /gif → chuyển thành GIF 5s đầu."""
    if not update.message:
        return
    msg = update.message

    # Cách 1: reply vào video đã gửi
    if msg.reply_to_message and msg.reply_to_message.video:
        await _process_gif(msg.reply_to_message, context)
        return
    if msg.reply_to_message and msg.reply_to_message.document and msg.reply_to_message.document.mime_type and "video" in msg.reply_to_message.document.mime_type:
        await _process_gif(msg.reply_to_message, context)
        return

    await msg.reply_text(
        "🎞️ <b>GIF MAKER</b>\n\n"
        "Cách dùng: <b>Reply</b> một video bất kỳ với lệnh <code>/gif</code>.\n\n"
        "• Bot sẽ lấy 5 giây đầu chuyển thành GIF.\n"
        "• Hỗ trợ video TikTok/YouTube đã tải qua bot.\n\n"
        "💡 <i>Tip: Tải video trước bằng cách gửi link, sau đó reply video đó với /gif.</i>",
        parse_mode=ParseMode.HTML,
    )


async def _process_gif(reply_msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tải video từ reply, chuyển sang GIF, gửi lại."""
    chat_id = reply_msg.chat_id
    progress = None
    video_path = None
    gif_path = None
    try:
        progress = TelegramProgress(
            context, chat_id,
            initial_text="Đang chuyển video sang GIF",
            title="🎞️ GIF MAKER",
        )
        await progress._create_task
        video = reply_msg.video
        if video is None and reply_msg.document and "video" in (reply_msg.document.mime_type or ""):
            video = reply_msg.document
        if video is None:
            raise ValueError("Không tìm thấy video để chuyển thành GIF")
        if getattr(video, "file_size", None) and video.file_size > MAX_IMAGE_FILE_SIZE * 5:
            raise MediaFileTooLargeError(video.file_size, MAX_IMAGE_FILE_SIZE * 5, "Video")
        tg_file = await context.bot.get_file(video.file_id)
        unique_id = uuid.uuid4().hex[:8]
        video_path = str(DOWNLOAD_DIR / f"gif_in_{unique_id}.mp4")
        await tg_file.download_to_drive(video_path)

        gif_path = str(DOWNLOAD_DIR / f"gif_out_{unique_id}.gif")
        await asyncio.to_thread(
            video_to_gif, video_path, gif_path, 0.0, 5.0, progress.update_sync
        )
        if os.path.getsize(gif_path) > MAX_PHOTO_FILE_SIZE * 2:
            raise MediaFileTooLargeError(
                os.path.getsize(gif_path), MAX_PHOTO_FILE_SIZE * 2, "GIF",
            )

        with open(gif_path, "rb") as f:
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=f,
                caption="🎞️ GIF đã sẵn sàng!",
            )
        try:
            await progress.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"GIF lỗi: {e}", exc_info=True)
        if progress and progress._message:
            try:
                await progress.fail("❌ Không thể chuyển video thành GIF. Video có thể quá dài!")
            except Exception:
                pass
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Không thể chuyển video thành GIF. Video có thể quá dài!",
            )
    finally:
        for p in [video_path, gif_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


async def meme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 4: /meme <top> | <bottom> — gửi ảnh → meme."""
    if not update.message:
        return
    args = " ".join(context.args) if context.args else ""
    if len(args) > MAX_TEXT_LENGTH:
        await update.message.reply_text("❌ Nội dung meme quá dài.")
        return

    if not args:
        await update.message.reply_text(
            "😂 <b>MEME GENERATOR</b>\n\n"
            "Cú pháp: <code>/meme &lt;text trên&gt; | &lt;text dưới&gt;</code>\n\n"
            "VD: <code>/meme KHI BẠN ĐANG CODE | VÀ BUG XUẤT HIỆN</code>\n\n"
            "📸 Sau đó gửi ảnh để tạo meme!",
            parse_mode=ParseMode.HTML,
        )
        return

    # Parse top | bottom
    if "|" in args:
        top, bottom = args.split("|", 1)
    else:
        top, bottom = args, ""

    context.user_data.update(mode="meme", mode_at=time.monotonic())
    context.user_data["args"] = {
        "top": top.strip(),
        "bottom": bottom.strip(),
    }
    await update.message.reply_text(
        f"😂 <b>MEME GENERATOR</b>\n\n"
        f"📝 Text trên: <b>{html.escape(top.strip()[:60])}</b>\n"
        f"📝 Text dưới: <b>{html.escape(bottom.strip()[:60]) if bottom.strip() else '(không có)'}</b>\n\n"
        "📸 Gửi ảnh để tạo meme!\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def compress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 5: /compress — gửi ảnh → nén."""
    if not update.message:
        return
    context.user_data.update(mode="compress", mode_at=time.monotonic())
    await update.message.reply_text(
        "📦 <b>IMAGE COMPRESSOR</b>\n\n"
        "📸 Gửi ảnh cần nén (mặc định: max 1280px, quality 60%)!\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def colors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 6: /colors — gửi ảnh → bảng màu chủ đạo."""
    if not update.message:
        return
    context.user_data.update(mode="colors", mode_at=time.monotonic())
    await update.message.reply_text(
        "🎨 <b>COLOR PALETTE</b>\n\n"
        "📸 Gửi ảnh cần phân tích màu!\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def watermark_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 7: /watermark <text> — gửi ảnh → watermark."""
    if not update.message:
        return
    args = " ".join(context.args) if context.args else ""
    if len(args) > MAX_TEXT_LENGTH:
        await update.message.reply_text("❌ Nội dung watermark quá dài.")
        return

    if not args:
        await update.message.reply_text(
            "💧 <b>WATERMARK</b>\n\n"
            "Cú pháp: <code>/watermark &lt;text&gt;</code>\n\n"
            "VD: <code>/watermark @MyChannel</code>\n\n"
            "📸 Sau đó gửi ảnh cần thêm watermark!",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data.update(mode="watermark", mode_at=time.monotonic())
    context.user_data["args"] = {"text": args.strip()[:50]}
    await update.message.reply_text(
        f"💧 <b>WATERMARK</b>\n\n"
        f"📝 Watermark: <b>{html.escape(args.strip()[:50])}</b>\n\n"
        "📸 Gửi ảnh cần thêm watermark!\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def thumb_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 8: /thumb <yt_url> — tải thumbnail YouTube."""
    if not update.message:
        return
    args = " ".join(context.args) if context.args else ""

    if not args:
        await update.message.reply_text(
            "🖼️ <b>YOUTUBE THUMBNAIL</b>\n\n"
            "Cú pháp: <code>/thumb &lt;link YouTube&gt;</code>\n\n"
            "VD: <code>/thumb https://youtu.be/dQw4w9WgXcQ</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    video_id = extract_youtube_id(args)
    if not video_id:
        await update.message.reply_text(
            "❌ Không nhận diện được link YouTube! Vui lòng thử lại.",
            parse_mode=ParseMode.HTML,
        )
        return

    output_path = str(DOWNLOAD_DIR / f"thumb_{uuid.uuid4().hex[:8]}.jpg")
    try:
        await asyncio.to_thread(get_youtube_thumbnail, video_id, output_path)
        if not os.path.exists(output_path) or os.path.getsize(output_path) > MAX_PHOTO_FILE_SIZE:
            raise MediaFileTooLargeError(
                os.path.getsize(output_path) if os.path.exists(output_path) else MAX_PHOTO_FILE_SIZE + 1,
                MAX_PHOTO_FILE_SIZE, "Ảnh",
            )
        with open(output_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"🖼️ Thumbnail YouTube (<code>{video_id}</code>)",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f"Thumbnail lỗi: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Không thể tải thumbnail. Video có thể không tồn tại!",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass


async def roll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 9: /roll [NdM] — xúc xắc animation native Telegram + kết quả."""
    if not update.message:
        return
    chat_id = update.effective_chat.id
    expression = " ".join(context.args) if context.args else "1d6"
    count, sides = parse_dice(expression)

    # Animation dice chỉ có mặt 6 (🎲) — chỉ animate khi đúng d6, tối đa 4 con
    rolls: list[int] = []
    animated = 0
    if sides == 6:
        for i in range(min(count, 4)):
            try:
                msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎲")
                # Telegram trả giá trị NGAY trong response — animation client
                # chỉ là hiệu ứng, số cuối cùng của animation chính là value này
                value = msg.dice.value if msg.dice else random.randint(1, 6)
                rolls.append(value)
                animated += 1
                if i < min(count, 4) - 1:
                    await asyncio.sleep(0.8)  # cách nhau cho animation dễ theo dõi
            except Exception:
                break
        if animated:
            # Đợi animation cuối chạy xong (~4s) rồi mới hiện kết quả
            await asyncio.sleep(4.0)

    # Roll phần còn lại (count > 4 hoặc dice != d6 → toàn bộ random)
    rolls += [random.randint(1, sides) for _ in range(count - animated)]

    total = sum(rolls)
    roll_str = " + ".join(str(r) for r in rolls)
    if count == 1:
        summary = f"🎲 <b>{total}</b>"
    else:
        summary = f"🎲 {roll_str} = <b>{total}</b>"

    try:
        await update.message.reply_text(summary, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def ascii_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOOL 10: /ascii — gửi ảnh → ASCII art."""
    if not update.message:
        return
    context.user_data.update(mode="ascii", mode_at=time.monotonic())
    await update.message.reply_text(
        "⌨️ <b>ASCII ART</b>\n\n"
        "📸 Gửi ảnh cần chuyển thành ASCII art!\n\n"
        "❌ Gõ /cancel để hủy.",
        parse_mode=ParseMode.HTML,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý ảnh gửi từ user — tùy chế độ đã kích hoạt."""
    if not update.message or not update.message.photo:
        return

    user = update.effective_user
    user_id = user.id if user else 0
    mode = context.user_data.get("mode")
    mode_at = context.user_data.get("mode_at")
    if mode and mode_at is not None and time.monotonic() - mode_at > 600:
        context.user_data.pop("mode", None)
        context.user_data.pop("mode_at", None)
        context.user_data.pop("args", None)
        mode = None

    if not mode:
        return
    if not _request_allowed(user_id):
        await update.message.reply_text("⏳ Bạn gửi yêu cầu quá nhanh. Vui lòng thử lại sau.")
        return

    context.user_data.pop("mode", None)
    context.user_data.pop("mode_at", None)
    args = context.user_data.pop("args", {})

    chat_id = update.effective_chat.id
    input_path = None
    output_path = None
    progress = None
    result_in_progress = False  # True nếu kết quả được edit vào progress msg (colors/ascii)

    try:
        # Mô tả chế độ đang xử lý
        mode_descriptions = {
            "enhance": "🔍 Đang làm nét ảnh...",
            "beautify": "✨ Đang làm đẹp ảnh...",
            "sticker": "🏷️ Đang tạo sticker...",
            "meme": "😂 Đang tạo meme...",
            "compress": "📦 Đang nén ảnh...",
            "watermark": "💧 Đang thêm watermark...",
            "colors": "🎨 Đang phân tích màu...",
            "ascii": "⌨️ Đang chuyển ASCII art...",
        }
        progress = TelegramProgress(
            context, chat_id,
            initial_text=mode_descriptions.get(mode, "Đang xử lý"),
            title="🛠️ IMAGE STUDIO",
        )
        await progress._create_task

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

        # Tải ảnh lớn nhất (photo[-1] là lớn nhất trong Telegram)
        photo = update.message.photo[-1]
        if photo.width * photo.height > MAX_IMAGE_PIXELS:
            raise ValueError("Ảnh có số lượng pixel vượt quá giới hạn.")
        if photo.file_size and photo.file_size > MAX_IMAGE_FILE_SIZE:
            raise MediaFileTooLargeError(photo.file_size, MAX_IMAGE_FILE_SIZE, "Ảnh đầu vào")
        photo_file = await context.bot.get_file(photo.file_id)

        unique_id = uuid.uuid4().hex[:8]
        input_path = str(DOWNLOAD_DIR / f"img_in_{unique_id}.jpg")
        output_path = str(DOWNLOAD_DIR / f"img_out_{unique_id}.jpg")

        await photo_file.download_to_drive(input_path)

        # ─── Xử lý theo từng mode ───────────────────────────────────────────
        if mode == "enhance":
            await asyncio.to_thread(enhance_image, input_path, output_path, progress.update_sync)
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="🔍 Ảnh đã được làm nét!")

        elif mode == "beautify":
            await asyncio.to_thread(beautify_image, input_path, output_path, progress.update_sync)
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="✨ Ảnh đã được làm đẹp!")

        elif mode == "sticker":
            await asyncio.to_thread(make_sticker, input_path, output_path)
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_sticker(chat_id=chat_id, sticker=f)

        elif mode == "meme":
            top = args.get("top", "")
            bottom = args.get("bottom", "")
            await asyncio.to_thread(add_meme_text, input_path, output_path, top, bottom)
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="😂 Meme đã sẵn sàng!")

        elif mode == "compress":
            _, size_before, size_after = await asyncio.to_thread(
                compress_image, input_path, output_path,
            )
            saved_pct = round((1 - size_after / size_before) * 100, 1) if size_before else 0
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=(
                        f"📦 Ảnh đã nén!\n"
                        f"💾 {size_before / 1024:.0f} KB → {size_after / 1024:.0f} KB "
                        f"(giảm {saved_pct}%)"
                    ),
                )

        elif mode == "watermark":
            wm_text = args.get("text", "@MyBot")
            await asyncio.to_thread(add_watermark, input_path, output_path, wm_text)
            _check_output_file(output_path, MAX_PHOTO_FILE_SIZE, "Ảnh kết quả")
            with open(output_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=f"💧 Đã thêm watermark: {html.escape(wm_text)}",
                )

        elif mode == "colors":
            colors = await asyncio.to_thread(extract_colors, input_path)
            lines = ["🎨 <b>BẢNG MÀU CHỦ ĐẠO</b>", ""]
            for c in colors:
                lines.append(f"▪️ <code>{c['hex']}</code> — {c['percent']}%")
            # Kết quả nằm trong chính progress message → KHÔNG delete
            await progress.finish("\n".join(lines))
            result_in_progress = True

        elif mode == "ascii":
            ascii_text = await asyncio.to_thread(image_to_ascii, input_path, 60)
            # Gửi trong <pre> — progress.finish giờ dùng HTML parse mode
            if len(ascii_text) > 3950:
                ascii_text = ascii_text[:3950]
            message = f"<pre>{html.escape(ascii_text)}</pre>"
            await progress.finish(message)
            result_in_progress = True

        # Xóa tin nhắn progress sau khi gửi kết quả (trừ khi kết quả nằm trong nó)
        if not result_in_progress:
            try:
                await progress.delete()
            except Exception:
                pass

        logger.info(f"Image {mode} thành công: user={user_id}")

    except Exception as e:
        logger.error(f"Lỗi xử lý ảnh {mode}: {e}", exc_info=True)
        error_text = "❌ Không thể xử lý ảnh. Vui lòng thử lại với ảnh khác!"
        if progress and progress._message:
            try:
                await progress.fail(error_text)
            except Exception:
                pass
        else:
            await update.message.reply_text(error_text)

    finally:
        # Dọn dẹp file tạm
        for path in [input_path, output_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


async def handle_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý tin nhắn chứa liên kết video, thực hiện tải và gửi video về người dùng."""
    message = update.effective_message
    if not message:
        return

    url = _extract_url_from_message(message)
    if not url:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return
    logger.info(
        "URL update received update_id=%s chat_id=%s user_id=%s",
        getattr(update, "update_id", "unknown"),
        chat_id,
        user.id if user else 0,
    )
    user_id = user.id if user else 0
    if not _request_allowed(user_id):
        await _safe_reply(message, "⏳ Bạn gửi yêu cầu quá nhanh. Vui lòng thử lại sau.")
        return

    try:
        await asyncio.wait_for(_download_slots.acquire(), timeout=0.25)
    except asyncio.TimeoutError:
        await _safe_reply(message, "⏳ Hệ thống đang xử lý các yêu cầu khác. Vui lòng thử lại sau.")
        return
    except Exception as exc:
        logger.error("Không acquire được slot tải media: %s", exc)
        await _safe_reply(message, "❌ Hệ thống đang bận. Vui lòng thử lại sau.")
        return

    job_acquired = True
    progress = None
    downloaded_files: list = []
    cleanup_dir = None
    result = None
    try:
        progress = TelegramProgress(
            context, chat_id,
            initial_text="Đã nhận link, đang kiểm tra",
            title="🎬 MEDIA DOWNLOADER",
        )
        try:
            await asyncio.wait_for(progress._create_task, timeout=10)
        except Exception as exc:
            logger.warning("Không chờ được progress message: %s", exc)
    except asyncio.CancelledError:
        if job_acquired:
            _download_slots.release()
        raise
    except Exception as exc:
        logger.error("Không khởi tạo được progress: %s", exc)
        if job_acquired:
            _download_slots.release()
        await _safe_reply(message, "❌ Không thể khởi tạo xử lý media. Vui lòng thử lại sau.")
        return

    try:
        url = await asyncio.wait_for(
            asyncio.to_thread(validate_media_url, url),
            timeout=URL_VALIDATION_TIMEOUT,
        )
    except asyncio.CancelledError:
        if job_acquired:
            _download_slots.release()
            job_acquired = False
        raise
    except InvalidURL as exc:
        await _report_progress_error(
            progress, message,
            f"⛔ URL không được phép: {html.escape(str(exc))}",
        )
        if job_acquired:
            _download_slots.release()
            job_acquired = False
        return
    except asyncio.TimeoutError:
        logger.warning("URL validation timeout update_id=%s", getattr(update, "update_id", "unknown"))
        await _report_progress_error(
            progress, message,
            "⏳ Kiểm tra link quá thời gian. Vui lòng thử lại sau.",
        )
        if job_acquired:
            _download_slots.release()
            job_acquired = False
        return
    except Exception as exc:
        logger.error("URL validation lỗi: %s", exc, exc_info=True)
        await _report_progress_error(
            progress, message,
            "❌ Không thể kiểm tra link. Vui lòng thử lại sau.",
        )
        if job_acquired:
            _download_slots.release()
            job_acquired = False
        return

    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception as exc:
        logger.error("Không phân tích được host URL: %s", exc)
        await _report_progress_error(progress, message, "❌ Link không hợp lệ.")
        if job_acquired:
            _download_slots.release()
            job_acquired = False
        return
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        platform = "TikTok"
    elif host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com"):
        platform = "Douyin"
    elif host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be"):
        platform = "YouTube"
    elif host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch" or host.endswith(".fb.watch"):
        platform = "Facebook"
    else:
        platform = "Other"
    await progress.configure(f"🎬 {platform}", source=host)
    await progress.set_stage("Đang lấy thông tin video", "Đang phân tích nguồn media...")

    try:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        except Exception as exc:
            logger.debug("Không gửi được chat action: %s", exc)

        # Gọi hàm tải video bất đồng bộ — truyền progress callback
        def _dl_progress(pct, text):
            progress.update_sync(pct, text)

        result = await extract_and_download(
            url, progress_cb=_dl_progress, _skip_validation=True,
        )
        cleanup_dir = getattr(result, "cleanup_dir", None)
        downloaded_files = list(result.paths)
        if getattr(result, "audio", None):
            downloaded_files.append(result.audio)
        title = result.title
        duration = result.duration

        # Chuẩn bị caption an toàn (giới hạn ký tự tối đa của Telegram là 1024)
        safe_title = html.escape(title)
        if len(safe_title) > 900:
            safe_title = safe_title[:900] + "..."
        caption = f"🎬 <b>{safe_title}</b>"
        if duration > 0:
            caption += f"\n⏱ <code>{_format_media_duration(duration)}</code>"

        # ── TikTok PHOTO POST: gửi album ảnh ──
        if result.kind == "photos":
            if len(result.paths) > MAX_PHOTO_COUNT:
                raise VideoDownloadError("Album vượt quá số lượng ảnh được phép.")
            for path in result.paths:
                _check_output_file(path, MAX_PHOTO_FILE_SIZE, "Ảnh")
            photo_size = sum(os.path.getsize(path) for path in result.paths)
            await progress.set_stage(
                "Đang gửi album lên Telegram",
                f"{len(result.paths)} ảnh • {_format_media_size(photo_size)}",
                pct=100,
            )
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
            for batch_start in range(0, len(result.paths), 10):
                batch_paths = result.paths[batch_start:batch_start + 10]
                handles = []
                try:
                    batch = []
                    for path in batch_paths:
                        file_handle = open(path, "rb")
                        handles.append(file_handle)
                        if batch_start == 0 and not batch:
                            batch.append(InputMediaPhoto(
                                file_handle, caption=caption, parse_mode=ParseMode.HTML,
                            ))
                        else:
                            batch.append(InputMediaPhoto(file_handle))
                    await context.bot.send_media_group(chat_id=chat_id, media=batch)
                finally:
                    for file_handle in handles:
                        file_handle.close()

            # Gửi kèm nhạc nền (photo post TikTok có music) nếu tải được
            audio = getattr(result, "audio", None)
            if audio and os.path.exists(audio):
                try:
                    if os.path.getsize(audio) > MAX_AUDIO_FILE_SIZE:
                        raise MediaFileTooLargeError(
                            os.path.getsize(audio), MAX_AUDIO_FILE_SIZE, "Audio",
                        )
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_AUDIO)
                    with open(audio, "rb") as af:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=af,
                            caption=f"🎵 Nhạc nền: {safe_title}",
                            parse_mode=ParseMode.HTML,
                        )
                except Exception as e:
                    logger.warning(f"Gửi nhạc nền TikTok fail: {e}")
        else:
            # ── VIDEO: gửi video như bình thường ──
            video_path = downloaded_files[0]
            if not os.path.isfile(video_path) or os.path.getsize(video_path) > MAX_FILE_SIZE:
                raise MediaFileTooLargeError(
                    os.path.getsize(video_path) if os.path.isfile(video_path) else MAX_FILE_SIZE + 1,
                    MAX_FILE_SIZE, "Video",
                )
            video_size = os.path.getsize(video_path)
            await progress.set_stage(
                "Đang gửi video lên Telegram",
                f"{_format_media_size(video_size)} • {_format_media_duration(duration)}",
                pct=100,
            )
            with open(video_path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    duration=duration if duration > 0 else None,
                    supports_streaming=True,
                    read_timeout=REQUEST_TIMEOUT,
                    write_timeout=REQUEST_TIMEOUT,
                )

        if result.kind == "photos":
            await progress.complete(
                "Đã gửi album thành công",
                f"{len(result.paths)} ảnh • {_format_media_size(photo_size)}",
            )
        else:
            await progress.complete(
                "Đã gửi video thành công",
                f"{_format_media_size(video_size)} • {_format_media_duration(duration)}",
            )

        # Ghi log thành công
        _log_activity(
            user_id=user.id if user else 0,
            username=user.username if user else "",
            first_name=user.first_name if user else "",
            url=url, platform=platform, status="✅",
        )

    except VideoTooLargeError as e:
        logger.warning("File quá lớn khi tải %s: %s", redact_url(url), e)
        _log_activity(
            user_id=user.id if user else 0,
            username=user.username if user else "",
            first_name=user.first_name if user else "",
            url=url, platform=platform, status="⚠️ Quá lớn",
        )
        error_text = (
            f"⚠️ <b>Không thể gửi video:</b>\n{str(e)}\n\n"
            "💡 <i>Gợi ý: Do giới hạn Telegram Bot API là 50MB, bạn hãy thử tải các video ngắn hơn hoặc độ phân giải thấp hơn.</i>"
        )
        await _report_progress_error(progress, message, error_text)

    except VideoDownloadError as e:
        logger.error("Lỗi tải video %s: %s", redact_url(url), e)
        _log_activity(
            user_id=user.id if user else 0,
            username=user.username if user else "",
            first_name=user.first_name if user else "",
            url=url, platform=platform, status="❌ Lỗi",
        )
        error_text = (
            "❌ <b>Không thể tải video từ liên kết này!</b>\n\n"
            "<b>Chi tiết lỗi backend:</b>\n"
            f"<pre>{html.escape(_safe_error_detail(e))}</pre>"
        )
        await _report_progress_error(progress, message, error_text)

    except Exception as e:
        logger.error(f"Lỗi không lường trước khi xử lý tin nhắn: {e}", exc_info=True)
        detail = _safe_error_detail(f"{type(e).__name__}: {e}")
        error_text = (
            "❌ <b>Đã xảy ra lỗi kỹ thuật:</b>\n"
            f"<pre>{html.escape(detail)}</pre>"
        )
        await _report_progress_error(progress, message, error_text)

    finally:
        for tmp_file in downloaded_files:
            _cleanup_path(tmp_file)
        if cleanup_dir:
            _cleanup_path(cleanup_dir)
        if job_acquired:
            _download_slots.release()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, asyncio.CancelledError):
        return
    if isinstance(error, BaseException):
        logger.error(
            "Ngoại lệ phát sinh khi xử lý update:",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        logger.error("Ngoại lệ phát sinh khi xử lý update: %s", error)
    message = getattr(update, "effective_message", None)
    if message:
        await _safe_reply(
            message,
            "❌ Đã xảy ra lỗi khi xử lý yêu cầu. Vui lòng thử lại sau!",
        )


def log_build_info() -> None:
    """Ghi thông tin phiên bản khi khởi động để đối chiếu với logs trên Render."""
    import subprocess
    import yt_dlp

    commit = (
        os.getenv("RENDER_GIT_COMMIT")
        or os.getenv("KOYEB_GIT_COMMIT")
        or os.getenv("GIT_COMMIT")
        or "unknown"
    )
    if commit == "unknown":
        try:
            commit = (
                subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip() or "unknown"
            )
        except Exception:
            commit = "unknown (no git in container)"

    cookies_configured = bool(
        os.getenv("YOUTUBE_COOKIES", "").strip()
        or os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    )
    proxy_configured = bool(os.getenv("YOUTUBE_PROXY", "").strip())
    provider_configured = bool(os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip())
    logger.info(
        f"BUILD INFO — commit: {commit} | yt-dlp: {yt_dlp.version.__version__} | "
        f"YT player clients: {YOUTUBE_PLAYER_CLIENTS} | "
        f"YouTube proxy: {'CẤU HÌNH' if proxy_configured else 'chưa có'} | "
        f"PO provider: {'CẤU HÌNH' if provider_configured else 'chưa có'} | "
        f"YouTube cookies: {'CẤU HÌNH' if cookies_configured else 'chưa có'}"
    )


class HealthCheckHandler(BaseHTTPRequestHandler):
    def _status_for_path(self) -> tuple[int, bytes]:
        path = self.path.split("?", 1)[0]
        if path in ("/ready", "/readyz", "/healthz"):
            if _bot_ready.is_set():
                return 200, b"ready"
            return 503, b"not ready"
        if path in ("/", "/health", "/livez"):
            return 200, b"Telegram Media Downloader Bot is running OK!"
        return 404, b"not found"

    def do_GET(self) -> None:
        status, body = self._status_for_path()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        status, _ = self._status_for_path()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


# Render gán URL public qua biến RENDER_EXTERNAL_URL
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
# Interval self-ping: 4 phút (Render spin-down sau 15 phút không traffic)
KEEPALIVE_INTERVAL = 240


def _keepalive_loop() -> None:
    """Self-ping URL public của Render để tránh free tier spin-down."""
    import urllib.request

    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        if not RENDER_EXTERNAL_URL:
            continue
        try:
            req = urllib.request.Request(
                RENDER_EXTERNAL_URL, headers={"User-Agent": "keepalive"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                logger.info(f"Keep-alive ping OK ({resp.status})")
        except Exception as e:
            logger.warning(f"Keep-alive ping thất bại: {e}")


def start_health_check_server(port: int) -> None:
    """Khởi chạy HTTP server phụ trợ trong luồng riêng để đáp ứng Render Port check."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Đã kích hoạt Health-Check HTTP server trên cổng {port} (Render compatible)")

        # Keep-alive: tự ping chính mình mỗi 4 phút để không bị spin-down
        if RENDER_EXTERNAL_URL:
            ka_thread = threading.Thread(target=_keepalive_loop, daemon=True)
            ka_thread.start()
            logger.info(f"Keep-alive đã bật — ping {RENDER_EXTERNAL_URL} mỗi {KEEPALIVE_INTERVAL}s")
        else:
            logger.info("Keep-alive tắt — chưa có RENDER_EXTERNAL_URL")
    except Exception as e:
        logger.warning(f"Không thể khởi chạy Health-check server trên cổng {port}: {e}")


async def _mark_application_ready(application) -> None:
    try:
        bot_info = await asyncio.wait_for(application.bot.get_me(), timeout=15)
        logger.info(
            "Telegram bot đã xác thực: @%s id=%s",
            getattr(bot_info, "username", "unknown"),
            getattr(bot_info, "id", "unknown"),
        )
    except Exception:
        _bot_ready.clear()
        logger.exception("Không thể xác thực BOT_TOKEN với Telegram")
        raise
    _bot_shutdown.clear()
    _bot_ready.set()


async def _mark_application_stopped(application) -> None:
    _bot_ready.clear()
    _bot_shutdown.set()


def main() -> None:
    """Điểm khởi chạy chính của Telegram Bot."""
    if not BOT_TOKEN:
        logger.critical(
            "KHÔNG TÌM THẤY BOT_TOKEN! Hãy thêm BOT_TOKEN vào file .env hoặc biến môi trường."
        )
        print("\n=======================================================")
        print("❌ LỖI: BOT_TOKEN chưa được cấu hình!")
        print("👉 Vui lòng tạo file .env với nội dung: BOT_TOKEN=your_token_here")
        print("   Hoặc chạy: export BOT_TOKEN='your_token_here'")
        print("=======================================================\n")
        raise SystemExit(1)

    logger.info("Đang khởi động Telegram Media Downloader Bot...")
    log_build_info()
    cleanup_stale_jobs()

    # Khởi chạy máy chủ HTTP Health Check nếu phát hiện biến PORT (Render Web Service)
    if PORT > 0:
        start_health_check_server(PORT)

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter(
            overall_max_rate=5.0,
            overall_time_period=1.0,
            group_max_rate=20,
            group_time_period=60.0,
            max_retries=2,
        ))
        .concurrent_updates(True)
        .post_init(_mark_application_ready)
        .post_shutdown(_mark_application_stopped)
        .build()
    )

    # Đăng ký Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("enhance", enhance_command))
    application.add_handler(CommandHandler("beautify", beautify_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("download", handle_video_url))
    application.add_handler(CommandHandler("dl", handle_video_url))
    # 10 Tools
    application.add_handler(CommandHandler("qr", qr_command))
    application.add_handler(CommandHandler("sticker", sticker_command))
    application.add_handler(CommandHandler("gif", gif_command))
    application.add_handler(CommandHandler("meme", meme_command))
    application.add_handler(CommandHandler("compress", compress_command))
    application.add_handler(CommandHandler("colors", colors_command))
    application.add_handler(CommandHandler("watermark", watermark_command))
    application.add_handler(CommandHandler("thumb", thumb_command))
    application.add_handler(CommandHandler("roll", roll_command))
    application.add_handler(CommandHandler("ascii", ascii_command))

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption) & ~filters.COMMAND,
            handle_video_url,
        ),
        group=0,
    )
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo),
        group=1,
    )

    # Đăng ký Global Error Handler
    application.add_error_handler(global_error_handler)

    logger.info("Bot đã sẵn sàng và đang lắng nghe tin nhắn...")
    # Polling — KHÔNG drop pending updates: tin nhắn gửi lúc bot restart
    # (deploy mới / wake từ sleep) vẫn được xử lý thay vì bị vứt bỏ
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

import os
import re
import html
import asyncio
import random
import threading
import time
import uuid
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, REQUEST_TIMEOUT, PORT, logger, DOWNLOAD_DIR
from downloader import (
    extract_and_download,
    VideoTooLargeError,
    VideoDownloadError,
    YOUTUBE_PLAYER_CLIENTS,
)
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

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN — Theo dõi user nào gửi link nào
# ═══════════════════════════════════════════════════════════════════════════════
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or "0")

# Activity log: deque tự xóa cũ nhất, giữ tối đa 200 entries
_activity_log: deque = deque(maxlen=200)


def _log_activity(
    user_id: int,
    username: str,
    first_name: str,
    url: str,
    platform: str,
    status: str,
) -> None:
    """Ghi lại hoạt động gửi link của user."""
    _activity_log.append({
        "time": time.time(),
        "user_id": user_id,
        "username": username or "-",
        "name": first_name or "-",
        "url": url[:80],
        "platform": platform,
        "status": status,
    })

# ═══════════════════════════════════════════════════════════════════════════════
#  IMAGE PROCESSING — User mode tracking
# ═══════════════════════════════════════════════════════════════════════════════
# _user_mode[user_id] = mode string
# Modes nhận ảnh: enhance, beautify, sticker, meme, compress, watermark, colors, ascii
# _user_args[user_id] = dict các tham số tùy chọn (meme text, watermark text, ...)
_user_mode: dict = {}
_user_args: dict = {}

# Regex phát hiện URL trong tin nhắn văn bản
URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lệnh /start - Chào mừng và hướng dẫn người dùng."""
    user = update.effective_user
    greeting = (
        f"👋 Xin chào <b>{html.escape(user.first_name if user else 'Bạn')}</b>!\n\n"
        "🤖 Tôi là <b>Media Downloader Bot</b> chuyên:\n"
        "• 🎵 <b>TikTok</b> (Tự động xóa Watermark / Logo)\n"
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
        "2️⃣ <b>Xử lý tự động:</b> Bot sẽ trích xuất luồng video tốt nhất, dùng FFmpeg gộp âm thanh và loại bỏ watermark.\n"
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
        f"📊 <b>ADMIN DASHBOARD</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📥 Tổng requests: <b>{total}</b> | ✅ {success} | ❌ {failed}",
        f"👤 Users: <b>{len(users)}</b>",
        "",
        f"👤 <b>DANH SÁCH USERS</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]

    for uid, info in sorted(users.items(), key=lambda x: -x[1]["count"]):
        uname = f"@{info['username']}" if info["username"] != "-" else f"ID: {uid}"
        lines.append(f"• {info['name']} ({uname}) — <b>{info['count']}</b> link")

    # 10 hoạt động gần nhất
    lines.append("")
    lines.append(f"📜 <b>10 HOẠT ĐỘNG GẦN NHẤT</b>")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")

    for entry in list(_activity_log)[-10:][::-1]:
        t = time.strftime("%d/%m %H:%M", time.localtime(entry["time"]))
        uname = f"@{entry['username']}" if entry["username"] != "-" else f"ID:{entry['user_id']}"
        url_short = entry["url"][:50] + ("..." if len(entry["url"]) > 50 else "")
        lines.append(
            f"{entry['status']} <code>{t}</code> {entry['name']} ({uname})\n"
            f"  🌐 {entry['platform']} — <code>{html.escape(url_short)}</code>"
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
    user_id = update.effective_user.id
    _user_mode[user_id] = "enhance"
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
    user_id = update.effective_user.id
    _user_mode[user_id] = "beautify"
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
    user_id = update.effective_user.id
    if user_id in _user_mode:
        del _user_mode[user_id]
        _user_args.pop(user_id, None)
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
    output_path = str(DOWNLOAD_DIR / f"qr_{uuid.uuid4().hex[:8]}.png")
    try:
        generate_qr(text, output_path)
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
    _user_mode[update.effective_user.id] = "sticker"
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
            initial_text="🎞️ <b>Đang chuyển video → GIF...</b>",
            title="🎞️ GIF Maker",
        )
        await progress._create_task
        video = reply_msg.video
        if video is None and reply_msg.document and "video" in (reply_msg.document.mime_type or ""):
            video = reply_msg.document
        if video is None:
            raise ValueError("Không tìm thấy video để chuyển thành GIF")
        tg_file = await context.bot.get_file(video.file_id)
        unique_id = uuid.uuid4().hex[:8]
        video_path = str(DOWNLOAD_DIR / f"gif_in_{unique_id}.mp4")
        await tg_file.download_to_drive(video_path)

        gif_path = str(DOWNLOAD_DIR / f"gif_out_{unique_id}.gif")
        await asyncio.to_thread(
            video_to_gif, video_path, gif_path, 0.0, 5.0, progress.update_sync
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

    _user_mode[update.effective_user.id] = "meme"
    _user_args[update.effective_user.id] = {
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
    _user_mode[update.effective_user.id] = "compress"
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
    _user_mode[update.effective_user.id] = "colors"
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

    if not args:
        await update.message.reply_text(
            "💧 <b>WATERMARK</b>\n\n"
            "Cú pháp: <code>/watermark &lt;text&gt;</code>\n\n"
            "VD: <code>/watermark @MyChannel</code>\n\n"
            "📸 Sau đó gửi ảnh cần thêm watermark!",
            parse_mode=ParseMode.HTML,
        )
        return

    _user_mode[update.effective_user.id] = "watermark"
    _user_args[update.effective_user.id] = {"text": args.strip()[:50]}
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
        get_youtube_thumbnail(video_id, output_path)
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
    _user_mode[update.effective_user.id] = "ascii"
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
    mode = _user_mode.get(user_id)

    if not mode:
        return  # User không ở chế độ nào → bỏ qua

    # Xóa mode & args ngay sau khi nhận ảnh
    del _user_mode[user_id]
    args = _user_args.pop(user_id, {})

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
            initial_text=f"<b>{mode_descriptions.get(mode, 'Đang xử lý...')}</b>",
        )
        await progress._create_task

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

        # Tải ảnh lớn nhất (photo[-1] là lớn nhất trong Telegram)
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        unique_id = uuid.uuid4().hex[:8]
        input_path = str(DOWNLOAD_DIR / f"img_in_{unique_id}.jpg")
        output_path = str(DOWNLOAD_DIR / f"img_out_{unique_id}.jpg")

        await photo_file.download_to_drive(input_path)

        # ─── Xử lý theo từng mode ───────────────────────────────────────────
        if mode == "enhance":
            await asyncio.to_thread(enhance_image, input_path, output_path, progress.update_sync)
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="🔍 Ảnh đã được làm nét!")

        elif mode == "beautify":
            await asyncio.to_thread(beautify_image, input_path, output_path, progress.update_sync)
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="✨ Ảnh đã được làm đẹp!")

        elif mode == "sticker":
            await asyncio.to_thread(make_sticker, input_path, output_path)
            with open(output_path, "rb") as f:
                await context.bot.send_sticker(chat_id=chat_id, sticker=f)

        elif mode == "meme":
            top = args.get("top", "")
            bottom = args.get("bottom", "")
            await asyncio.to_thread(add_meme_text, input_path, output_path, top, bottom)
            with open(output_path, "rb") as f:
                await context.bot.send_photo(chat_id=chat_id, photo=f, caption="😂 Meme đã sẵn sàng!")

        elif mode == "compress":
            _, size_before, size_after = await asyncio.to_thread(
                compress_image, input_path, output_path,
            )
            saved_pct = round((1 - size_after / size_before) * 100, 1) if size_before else 0
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
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = URL_REGEX.search(text)
    if not match:
        return

    url = match.group(0)
    chat_id = update.effective_chat.id
    progress = None
    downloaded_files: list = []

    # Xác định nền tảng
    user = update.effective_user
    if "tiktok.com" in url.lower() or "vm.tiktok.com" in url.lower():
        platform = "TikTok"
    elif "youtube.com" in url.lower() or "youtu.be" in url.lower():
        platform = "YouTube"
    elif "facebook.com" in url.lower() or "fb.watch" in url.lower():
        platform = "Facebook"
    else:
        platform = "Other"

    try:
        # Gửi tin nhắn tiến trình — tự cập nhật % trong lúc tải
        progress = TelegramProgress(
            context, chat_id,
            initial_text="⏳ <b>Đang tải và xử lý media...</b>",
            title=f"🎬 {platform}",
        )
        # Đợi message được tạo (create_task trong __init__)
        await progress._create_task

        # Hiển thị hành động bot đang tải video
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        # Gọi hàm tải video bất đồng bộ — truyền progress callback
        def _dl_progress(pct, text):
            progress.update_sync(pct, text)

        result = await extract_and_download(url, progress_cb=_dl_progress)
        downloaded_files = result.paths
        title = result.title
        duration = result.duration

        # Chuẩn bị caption an toàn (giới hạn ký tự tối đa của Telegram là 1024)
        safe_title = html.escape(title)
        if len(safe_title) > 900:
            safe_title = safe_title[:900] + "..."
        caption = f"🎬 <b>{safe_title}</b>"

        # ── TikTok PHOTO POST: gửi album ảnh ──
        if result.kind == "photos":
            try:
                await progress.finish("🖼️ <b>Đang tải ảnh lên Telegram...</b>")
            except Exception:
                pass
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

            # Media group tối đa 10 ảnh/group → chia batch
            handles = []
            try:
                batch = []
                for idx, path in enumerate(result.paths):
                    fh = open(path, "rb")
                    handles.append(fh)
                    if idx == 0:
                        batch.append(InputMediaPhoto(fh, caption=caption, parse_mode=ParseMode.HTML))
                    else:
                        batch.append(InputMediaPhoto(fh))
                    if len(batch) == 10:
                        await context.bot.send_media_group(chat_id=chat_id, media=batch)
                        batch = []
                if batch:
                    await context.bot.send_media_group(chat_id=chat_id, media=batch)
            finally:
                for fh in handles:
                    try:
                        fh.close()
                    except Exception:
                        pass
        else:
            # ── VIDEO: gửi video như bình thường ──
            # Cập nhật trạng thái đang upload
            try:
                await progress.finish("🚀 <b>Đang tải video lên Telegram...</b>")
            except Exception:
                pass

            with open(downloaded_files[0], "rb") as video_file:
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

        # Xóa tin nhắn trạng thái sau khi gửi thành công
        try:
            await progress.delete()
        except Exception:
            pass

        # Ghi log thành công
        _log_activity(
            user_id=user.id if user else 0,
            username=user.username if user else "",
            first_name=user.first_name if user else "",
            url=url, platform=platform, status="✅",
        )

    except VideoTooLargeError as e:
        logger.warning(f"File quá lớn khi tải {url}: {e}")
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
        if progress and progress._message:
            await progress.fail(error_text)
        else:
            await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    except VideoDownloadError as e:
        logger.error(f"Lỗi tải video {url}: {e}")
        _log_activity(
            user_id=user.id if user else 0,
            username=user.username if user else "",
            first_name=user.first_name if user else "",
            url=url, platform=platform, status="❌ Lỗi",
        )
        # Hiển thị phần lỗi gốc để người dùng và dev biết nguyên nhân thật (IP bị chặn, extractor lỗi...)
        reason = str(e).replace("Lỗi tải video từ nền tảng: ", "").strip()
        if len(reason) > 300:
            reason = reason[:300] + "..."
        error_text = (
            "❌ <b>Không thể tải video từ liên kết này!</b>\n\n"
            f"<i>Chi tiết: {html.escape(reason)}</i>\n\n"
            "Vui lòng kiểm tra lại:\n"
            "• Đảm bảo liên kết chính xác và có thể truy cập công khai.\n"
            "• Video không bị khóa riêng tư hoặc giới hạn độ tuổi."
        )
        if progress and progress._message:
            await progress.fail(error_text)
        else:
            await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Lỗi không lường trước khi xử lý tin nhắn: {e}", exc_info=True)
        error_text = "❌ Đã có sự cố kỹ thuật xảy ra trong quá trình xử lý. Vui lòng thử lại sau!"
        if progress and progress._message:
            try:
                await progress.fail(error_text)
            except Exception:
                pass
        else:
            await update.message.reply_text(error_text)

    finally:
        # Tự động dọn dẹp file tạm thời để tránh tràn dung lượng ổ đĩa
        for tmp_file in downloaded_files:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                    logger.info(f"Đã dọn dẹp file tạm thành công: {tmp_file}")
                except OSError as cleanup_err:
                    logger.warning(f"Không thể xóa file tạm {tmp_file}: {cleanup_err}")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bắt các lỗi ngoại lệ toàn cục để ngăn bot crash."""
    logger.error("Ngoại lệ phát sinh khi xử lý update:", exc_info=context.error)


def log_build_info() -> None:
    """Ghi thông tin phiên bản khi khởi động để đối chiếu với logs trên Render."""
    import subprocess
    import yt_dlp

    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "unknown"
        )
    except Exception:
        commit = "unknown (no git in container)"

    cookies_configured = bool(os.getenv("YOUTUBE_COOKIES", "").strip())
    logger.info(
        f"BUILD INFO — commit: {commit} | yt-dlp: {yt_dlp.version.__version__} | "
        f"YT player clients: {YOUTUBE_PLAYER_CLIENTS} | "
        f"YouTube cookies: {'CẤU HÌNH' if cookies_configured else 'chưa có'}"
    )


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Handler phục vụ health check HTTP của Render để giữ bot hoạt động."""
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram Media Downloader Bot is running OK!")

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Giảm thiểu ghi log định kỳ của health check
        pass


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
        return

    logger.info("Đang khởi động Telegram Media Downloader Bot...")
    log_build_info()

    # Khởi chạy máy chủ HTTP Health Check nếu phát hiện biến PORT (Render Web Service)
    if PORT > 0:
        start_health_check_server(PORT)

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Đăng ký Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("enhance", enhance_command))
    application.add_handler(CommandHandler("beautify", beautify_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
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

    # Đăng ký Photo Handler — bắt ảnh trước khi URL handler
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    # Đăng ký Message Handler bắt link HTTP/HTTPS
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url)
    )

    # Đăng ký Global Error Handler
    application.add_error_handler(global_error_handler)

    logger.info("Bot đã sẵn sàng và đang lắng nghe tin nhắn...")
    # Polling — KHÔNG drop pending updates: tin nhắn gửi lúc bot restart
    # (deploy mới / wake từ sleep) vẫn được xử lý thay vì bị vứt bỏ
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

import os
import re
import html
import asyncio
import threading
import time
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, REQUEST_TIMEOUT, PORT, logger
from downloader import (
    extract_and_download,
    VideoTooLargeError,
    VideoDownloadError,
    YOUTUBE_PLAYER_CLIENTS,
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

# Regex phát hiện URL trong tin nhắn văn bản
URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lệnh /start - Chào mừng và hướng dẫn người dùng."""
    user = update.effective_user
    greeting = (
        f"👋 Xin chào <b>{html.escape(user.first_name if user else 'Bạn')}</b>!\n\n"
        "🤖 Tôi là <b>Media Downloader Bot</b> chuyên tải video chất lượng cao từ:\n"
        "• 🎵 <b>TikTok</b> (Tự động xóa Watermark / Logo)\n"
        "• 📘 <b>Facebook</b> (Chất lượng HD cao nhất)\n"
        "• 📺 <b>YouTube</b> (Video kèm âm thanh đầy đủ)\n\n"
        "📌 <b>Cách sử dụng:</b> Đơn giản chỉ cần sao chép và gửi trực tiếp link video vào đây!\n\n"
        "⚠️ <i>Lưu ý: Telegram Bot tiêu chuẩn giới hạn file gửi tối đa <b>50MB</b>.</i>"
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
    status_msg = None
    downloaded_file = None

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
        # Gửi tin nhắn phản hồi ban đầu
        status_msg = await update.message.reply_text(
            "⏳ <b>Đang tải và xử lý video...</b> Vui lòng đợi trong giây lát.",
            parse_mode=ParseMode.HTML,
        )

        # Hiển thị hành động bot đang tải video
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        # Gọi hàm tải video bất đồng bộ
        file_path, title, duration = await extract_and_download(url)
        downloaded_file = file_path

        # Chuẩn bị caption an toàn (giới hạn ký tự tối đa của Telegram là 1024)
        safe_title = html.escape(title)
        if len(safe_title) > 900:
            safe_title = safe_title[:900] + "..."

        caption = f"🎬 <b>{safe_title}</b>"

        # Cập nhật trạng thái đang upload
        try:
            await status_msg.edit_text(
                "🚀 <b>Đang tải video lên Telegram...</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Gửi video về cho người dùng
        with open(downloaded_file, "rb") as video_file:
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
        if status_msg:
            try:
                await status_msg.delete()
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
        if status_msg:
            await status_msg.edit_text(error_text, parse_mode=ParseMode.HTML)
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
        if status_msg:
            await status_msg.edit_text(error_text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Lỗi không lường trước khi xử lý tin nhắn: {e}", exc_info=True)
        error_text = "❌ Đã có sự cố kỹ thuật xảy ra trong quá trình xử lý. Vui lòng thử lại sau!"
        if status_msg:
            try:
                await status_msg.edit_text(error_text)
            except Exception:
                pass
        else:
            await update.message.reply_text(error_text)

    finally:
        # Tự động dọn dẹp file video tạm thời để tránh tràn dung lượng ổ đĩa
        if downloaded_file and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
                logger.info(f"Đã dọn dẹp file tạm thành công: {downloaded_file}")
            except OSError as cleanup_err:
                logger.warning(f"Không thể xóa file tạm {downloaded_file}: {cleanup_err}")


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


def start_health_check_server(port: int) -> None:
    """Khởi chạy HTTP server phụ trợ trong luồng riêng để đáp ứng Render Port check."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Đã kích hoạt Health-Check HTTP server trên cổng {port} (Render compatible)")
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

    # Đăng ký Message Handler bắt link HTTP/HTTPS
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_url)
    )

    # Đăng ký Global Error Handler
    application.add_error_handler(global_error_handler)

    logger.info("Bot đã sẵn sàng và đang lắng nghe tin nhắn...")
    # Chạy polling với cấu hình an toàn
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

import os
import sys
import logging
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Dự phòng đọc trực tiếp từ .env nếu chưa cài thư viện python-dotenv
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.is_file():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except Exception:
            pass

# Đảm bảo hiển thị Unicode/UTF-8 an toàn trên mọi hệ điều hành (bao gồm Windows Command Prompt/PowerShell)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Cấu hình Logging
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    format=LOG_FORMAT,
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    stream=sys.stdout
)
logger = logging.getLogger("telegram-media-downloader")

# Token của Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    logger.warning(
        "[CẢNH BÁO] Biến môi trường BOT_TOKEN chưa được thiết lập! "
        "Bot sẽ không thể kết nối đến Telegram API nếu không có token hợp lệ. "
        "Hãy tạo file .env hoặc export BOT_TOKEN=<your_token>."
    )

# Giới hạn dung lượng tệp tin (Telegram Bot API tiêu chuẩn cho phép gửi tối đa 50MB)
# Đặt ngưỡng an toàn là 49MB để tránh lỗi biên độ
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 MB tính theo bytes

# Thư mục chứa các tệp tải về tạm thời
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Timeout cấu hình cho Telegram upload (giây)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "180"))

# Cổng mạng phục vụ Health Check (tự động nhận diện trên Render / Cloud Web Service)
PORT = int(os.getenv("PORT", "0"))

import os
import sys
import logging
from pathlib import Path


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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

MAX_FILE_SIZE_MB = _env_int("MAX_FILE_SIZE_MB", 49, 1, 50)
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_PHOTO_FILE_SIZE = _env_int("MAX_PHOTO_FILE_SIZE_MB", 10, 1, 10) * 1024 * 1024
MAX_AUDIO_FILE_SIZE = _env_int("MAX_AUDIO_FILE_SIZE_MB", 49, 1, 49) * 1024 * 1024
MAX_IMAGE_FILE_SIZE = _env_int("MAX_IMAGE_FILE_SIZE_MB", 20, 1, 50) * 1024 * 1024
MAX_IMAGE_PIXELS = _env_int("MAX_IMAGE_PIXELS", 25_000_000, 1_000_000, 100_000_000)
MAX_IMAGE_DIMENSION = _env_int("MAX_IMAGE_DIMENSION", 4096, 256, 8192)
MAX_PHOTO_COUNT = _env_int("MAX_PHOTO_COUNT", 30, 1, 50)
MAX_ALBUM_DURATION = _env_int("MAX_ALBUM_DURATION", 180, 10, 600)
MAX_VIDEO_DURATION = _env_int("MAX_VIDEO_DURATION", 600, 10, 3600)
MAX_CONCURRENT_JOBS = _env_int("MAX_CONCURRENT_JOBS", 2, 1, 8)
USER_RATE_LIMIT_SECONDS = _env_int("USER_RATE_LIMIT_SECONDS", 5, 0, 300)
MAX_TEXT_LENGTH = _env_int("MAX_TEXT_LENGTH", 500, 32, 2000)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).expanduser()
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DOWNLOAD_DIR = BASE_DIR / "downloads"

REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 180, 30, 600)
PORT = _env_int("PORT", 0, 0, 65535)
ADMIN_USER_ID = _env_int("ADMIN_USER_ID", 0, 0, 2**63 - 1)
ALLOW_PRIVATE_URLS = _env_bool("ALLOW_PRIVATE_URLS", False)

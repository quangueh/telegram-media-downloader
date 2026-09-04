import os
import uuid
import asyncio
from pathlib import Path
from typing import Tuple, Optional, Union
import yt_dlp

from config import MAX_FILE_SIZE, DOWNLOAD_DIR, logger


class DownloaderError(Exception):
    """Lớp cơ sở cho các lỗi trong quá trình tải video."""
    pass


class VideoTooLargeError(DownloaderError):
    """Ngoại lệ nảy sinh khi kích thước video vượt quá giới hạn Telegram API (50MB)."""
    def __init__(self, size_bytes: int, max_bytes: int = MAX_FILE_SIZE):
        self.size_mb = size_bytes / (1024 * 1024)
        self.max_mb = max_bytes / (1024 * 1024)
        super().__init__(
            f"Dung lượng video ({self.size_mb:.1f}MB) vượt quá giới hạn cho phép của Telegram Bot ({self.max_mb:.0f}MB)."
        )


class VideoDownloadError(DownloaderError):
    """Lỗi khi không thể tải hoặc xử lý video bằng yt-dlp."""
    pass


def _cleanup_leftovers(directory: Path, unique_id: str) -> None:
    """Xóa tất cả các file tạm có chứa unique_id khi xảy ra lỗi."""
    try:
        for f in directory.glob(f"*{unique_id}*"):
            if f.is_file():
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass


def _sync_extract_and_download(url: str, output_dir: Union[str, Path]) -> Tuple[str, str, int]:
    """
    Hàm đồng bộ thực thi tải video bằng yt-dlp và gộp audio/video qua ffmpeg.
    
    Returns:
        Tuple[str, str, int]: (đường dẫn file đã tải, tiêu đề video, thời lượng tính bằng giây)
    """
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    unique_id = uuid.uuid4().hex[:10]
    outtmpl = str(target_dir / f"%(title).50s_{unique_id}.%(ext)s")

    downloaded_filepath: Optional[str] = None

    def postprocessor_hook(info: dict) -> None:
        nonlocal downloaded_filepath
        if info.get("status") == "finished":
            downloaded_filepath = info.get("info_dict", {}).get("_filename") or info.get("filepath")

    ydl_opts = {
        # Ưu tiên video chất lượng cao nhất mp4 kèm âm thanh tốt nhất m4a, tự động fallback về mp4/best
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        # Tự động gộp video và audio thành định dạng mp4 tương thích tốt nhất với Telegram
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        # Giới hạn kích thước tối đa trong tùy chọn của yt-dlp để từ chối sớm nếu biết trước kích thước
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Giả lập User-Agent hiện đại để tránh bị chặn bởi TikTok/Facebook/YouTube
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        },
        # Tối ưu hóa cho TikTok không watermark: yt-dlp mặc định lấy direct stream HD không logo
        "postprocessor_hooks": [postprocessor_hook],
    }

    logger.info(f"Bắt đầu phân tích và tải URL: {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Bước 1: Trích xuất metadata trước để kiểm tra kích thước ước tính
            info_dict = ydl.extract_info(url, download=False)
            if not info_dict:
                raise VideoDownloadError("Không thể lấy thông tin từ URL được cung cấp.")

            # Kiểm tra kích thước ước lượng nếu có sẵn
            estimated_size = info_dict.get("filesize") or info_dict.get("filesize_approx")
            if estimated_size and estimated_size > MAX_FILE_SIZE:
                raise VideoTooLargeError(estimated_size, MAX_FILE_SIZE)

            title = info_dict.get("title", "Video không có tiêu đề")
            duration = int(info_dict.get("duration") or 0)

            # Bước 2: Thực hiện tải và xử lý qua ffmpeg
            download_info = ydl.extract_info(url, download=True)

            # Xác định đường dẫn file thực tế sau khi tải và merge
            final_file = downloaded_filepath or ydl.prepare_filename(download_info)
            
            # Đảm bảo phần mở rộng là .mp4 sau khi merge nếu có
            if not os.path.exists(final_file):
                base_without_ext = os.path.splitext(final_file)[0]
                mp4_variant = f"{base_without_ext}.mp4"
                if os.path.exists(mp4_variant):
                    final_file = mp4_variant
                else:
                    # Tìm file tương ứng trong thư mục output
                    candidates = [
                        f for f in target_dir.glob(f"*{unique_id}*")
                        if not f.name.endswith((".part", ".ytdl", ".temp"))
                    ]
                    if candidates:
                        final_file = str(candidates[0])
                    else:
                        raise VideoDownloadError("Không tìm thấy tệp video sau khi tải xuống.")

            # Bước 3: Kiểm tra kích thước file thực tế sau khi tải
            actual_size = os.path.getsize(final_file)
            if actual_size > MAX_FILE_SIZE:
                # Xóa ngay file vượt dung lượng để tránh đầy ổ cứng
                try:
                    os.remove(final_file)
                except OSError:
                    pass
                raise VideoTooLargeError(actual_size, MAX_FILE_SIZE)

            logger.info(f"Tải thành công: {final_file} (Kích thước: {actual_size / (1024*1024):.2f}MB)")
            return final_file, title, duration

    except yt_dlp.utils.MaxDownloadsReached:
        _cleanup_leftovers(target_dir, unique_id)
        raise VideoDownloadError("Đã đạt giới hạn số lượt tải tối đa.")
    except yt_dlp.utils.DownloadError as e:
        _cleanup_leftovers(target_dir, unique_id)
        err_msg = str(e).lower()
        if "file is larger than max-filesize" in err_msg or "larger than max_filesize" in err_msg:
            raise VideoTooLargeError(MAX_FILE_SIZE + 1, MAX_FILE_SIZE)
        raise VideoDownloadError(f"Lỗi tải video từ nền tảng: {e}")
    except VideoTooLargeError:
        _cleanup_leftovers(target_dir, unique_id)
        raise
    except Exception as e:
        _cleanup_leftovers(target_dir, unique_id)
        logger.error(f"Lỗi không xác định khi tải video: {e}", exc_info=True)
        raise VideoDownloadError(f"Đã xảy ra sự cố khi tải video: {str(e)}")


async def extract_and_download(
    url: str, output_path: Optional[Union[str, Path]] = None
) -> Tuple[str, str, int]:
    """
    Hàm wrapper bất đồng bộ để gọi _sync_extract_and_download mà không gây nghẽn Event Loop.
    
    Args:
        url (str): Liên kết video (YouTube, TikTok, Facebook,...)
        output_path (Optional[Union[str, Path]]): Thư mục lưu tệp tải về
        
    Returns:
        Tuple[str, str, int]: (file_path, title, duration)
    """
    target_dir = Path(output_path) if output_path else DOWNLOAD_DIR
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _sync_extract_and_download, url, target_dir
    )

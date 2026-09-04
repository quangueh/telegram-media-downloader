import os
import sys
import unittest
from pathlib import Path

# Thêm thư mục dự án vào sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import MAX_FILE_SIZE, DOWNLOAD_DIR
from downloader import VideoTooLargeError, VideoDownloadError, _sync_extract_and_download


class TestDownloaderComponents(unittest.TestCase):
    """Kiểm tra các thành phần cấu hình và ngoại lệ của downloader."""

    def test_config_constants(self):
        """Kiểm tra giới hạn 49MB và thư mục downloads."""
        self.assertEqual(MAX_FILE_SIZE, 49 * 1024 * 1024)
        self.assertTrue(DOWNLOAD_DIR.exists())

    def test_video_too_large_exception(self):
        """Kiểm tra ngoại lệ VideoTooLargeError format đúng thông điệp."""
        fake_size = 55 * 1024 * 1024  # 55 MB
        err = VideoTooLargeError(fake_size, MAX_FILE_SIZE)
        self.assertIn("55.0MB", str(err))
        self.assertIn("49MB", str(err))

    def test_cleanup_leftovers(self):
        """Kiểm tra dọn dẹp các tệp tin tạm chứa unique_id."""
        from downloader import _cleanup_leftovers
        import uuid
        test_id = f"test_{uuid.uuid4().hex[:6]}"
        temp_file = DOWNLOAD_DIR / f"temp_{test_id}.part"
        temp_file.write_text("test data")
        self.assertTrue(temp_file.exists())
        _cleanup_leftovers(DOWNLOAD_DIR, test_id)
        self.assertFalse(temp_file.exists())

    def test_health_check_server(self):
        """Kiểm tra Health Check HTTP Server phục vụ Render."""
        from bot import start_health_check_server
        import urllib.request
        import time

        test_port = 19998
        start_health_check_server(test_port)
        time.sleep(0.2)
        with urllib.request.urlopen(f"http://127.0.0.1:{test_port}/") as resp:
            self.assertEqual(resp.status, 200)
            data = resp.read().decode("utf-8")
            self.assertIn("running OK", data)


if __name__ == "__main__":
    unittest.main()

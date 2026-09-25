import os
import sys
import asyncio
import shutil
import subprocess
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

# Thêm thư mục dự án vào sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import yt_dlp

from config import MAX_FILE_SIZE, DOWNLOAD_DIR
from security import InvalidURL, redact_url, validate_public_url
from bot import _extract_url_from_message
from downloader import (
    VideoTooLargeError,
    VideoDownloadError,
    DownloaderError,
    IncompleteDownloadError,
    MediaResult,
    _sync_extract_and_download,
    _ensure_playable,
    _probe_stream_codec,
    _probe_container,
    _is_mp4_container,
    _safe_filename,
    _fmt_mb,
    _is_youtube_blocked,
    _is_douyin_url,
    _is_tiktok_url,
    _load_cookies,
    _select_piped_streams,
)

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def asyncio_run(coro):
    """Chạy coroutine trong event loop (đã có running loop → trả thẳng)."""
    try:
        return asyncio.get_running_loop().run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _ffmpeg(args):
    return subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error"] + args, capture_output=True
    )


def _gen_h264(path):
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ])


def _gen_vp9(path):
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
        "-c:v", "libvpx-vp9", "-an", str(path),
    ])


def _gen_av1(path):
    _ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
        "-c:v", "libaom-av1", "-an", "-cpu-used", "8", str(path),
    ])


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


class TestPlayableCompatibility(unittest.TestCase):
    """Kiểm tra logic đảm bảo video phát được trên mọi thiết bị (H.264 + AAC + mp4)."""

    def test_is_mp4_container(self):
        """Nhận diện container họ MP4 (ffprobe trả list phân tách phẩy)."""
        self.assertTrue(_is_mp4_container("mov,mp4,m4a,3gp,3g2,mj2"))
        self.assertTrue(_is_mp4_container("mp4"))
        self.assertTrue(_is_mp4_container("mov"))
        self.assertFalse(_is_mp4_container("matroska,webm"))
        self.assertFalse(_is_mp4_container(""))

    def test_incomplete_download_error_is_downloader_error(self):
        """IncompleteDownloadError phải kế thừa DownloaderError."""
        self.assertTrue(issubclass(IncompleteDownloadError, DownloaderError))
        self.assertEqual(str(IncompleteDownloadError("abc")), "abc")

    def test_safe_filename_sanitizes(self):
        """Ký tự không hợp lệ trong tên file phải bị loại bỏ."""
        self.assertEqual(
            _safe_filename('a/b\\c:d*e?"f<>g|h\n\r\t', "u1"),
            "a b c d e f g h",
        )
        self.assertEqual(_safe_filename("   ", "u1"), "video")

    def test_fmt_mb(self):
        self.assertEqual(_fmt_mb(1048576), "1.0MB")

    @unittest.skipUnless(FFMPEG_AVAILABLE, "cần ffmpeg/ffprobe")
    def test_h264_aac_mp4_no_reencode(self):
        """File H.264 + AAC trong mp4 phải được giữ nguyên (không re-encode)."""
        src = DOWNLOAD_DIR / "test_h264_ok.mp4"
        try:
            _gen_h264(src)
            self.assertEqual(_probe_stream_codec(str(src), "v:0"), "h264")
            before = src.read_bytes()
            out = _ensure_playable(str(src))
            self.assertEqual(out, str(src))
            self.assertEqual(src.read_bytes(), before)
        finally:
            if src.exists():
                src.unlink(missing_ok=True)

    @unittest.skipUnless(FFMPEG_AVAILABLE, "cần ffmpeg/ffprobe")
    def test_vp9_reencoded_to_h264(self):
        """File VP9 (nhét mp4) phải được re-encode sang H.264 + AAC."""
        src = DOWNLOAD_DIR / "test_vp9.mp4"
        try:
            _gen_vp9(src)
            self.assertEqual(_probe_stream_codec(str(src), "v:0"), "vp9")
            out = _ensure_playable(str(src))
            self.assertEqual(_probe_stream_codec(out, "v:0"), "h264")
            self.assertTrue(_is_mp4_container(_probe_container(out)))
        finally:
            if src.exists():
                src.unlink(missing_ok=True)

    @unittest.skipUnless(FFMPEG_AVAILABLE, "cần ffmpeg/ffprobe")
    def test_av1_reencoded_to_h264(self):
        """File AV1 (nhét mp4) phải được re-encode sang H.264 + AAC."""
        src = DOWNLOAD_DIR / "test_av1.mp4"
        try:
            _gen_av1(src)
            if _probe_stream_codec(str(src), "v:0") != "av1":
                self.skipTest("ffmpeg không có encoder libaom-av1")
            out = _ensure_playable(str(src))
            self.assertEqual(_probe_stream_codec(out, "v:0"), "h264")
            self.assertTrue(_is_mp4_container(_probe_container(out)))
        finally:
            if src.exists():
                src.unlink(missing_ok=True)


class TestDownloaderRouting(unittest.TestCase):
    """Kiểm tra router không để lỗi yt-dlp thoát ra ngoài."""

    def test_ytdlp_download_error_wrapped(self):
        """DownloadError của yt-dlp phải được wrap thành VideoDownloadError."""
        opts = {"quiet": True, "no_warnings": True}
        with mock.patch("downloader._build_ytdlp_opts", return_value=opts):
            with mock.patch.object(
                yt_dlp.YoutubeDL,
                "extract_info",
                side_effect=yt_dlp.utils.DownloadError(
                    "ERROR: [generic] x: Unable to download webpage: HTTP Error 404"
                ),
            ):
                with self.assertRaises(VideoDownloadError):
                    _sync_extract_and_download(
                        "https://example.com/x", DOWNLOAD_DIR
                    )

    def test_video_too_large_not_wrapped(self):
        """VideoTooLargeError phải không bị wrap thành DownloadError."""
        opts = {"quiet": True, "no_warnings": True}
        with mock.patch("downloader._build_ytdlp_opts", return_value=opts):
            with mock.patch.object(
                yt_dlp.YoutubeDL,
                "extract_info",
                side_effect=VideoTooLargeError(50 * 1024 * 1024, MAX_FILE_SIZE),
            ):
                with self.assertRaises(VideoTooLargeError):
                    _sync_extract_and_download(
                        "https://example.com/x", DOWNLOAD_DIR
                    )


class TestTikTokPhotoPost(unittest.TestCase):
    """TikTok bài đăng ảnh phải tải ảnh (kind='photos') thay vì xử lý như video."""

    def _fake_download(self, dest):
        return dest

    async def _run(self, images, music_url="https://example.com/music.mp3", video_codec="", render_ok=False):
        import contextlib
        from downloader import _download_via_tikwm

        async def fake_stream(url, dest, **kwargs):
            data = b"fake-music-xx" if "music" in url else b"fake-image"
            Path(dest).write_bytes(data)
            return len(data)

        def fake_probe(path, stream):
            # media video slideshow → v:0 trả codec; còn lại coi như audio/không video
            if video_codec and stream == "v:0" and "_media" in str(path):
                return video_codec
            return ""

        def fake_render(image_paths, audio_path, output_path, duration):
            Path(output_path).write_bytes(b"fake-video")
            return output_path

        info = {
            "title": "Photo album test",
            "images": images,
            "play": music_url,
            "music": music_url,
            "duration": 15,
            "size": 0,
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("downloader._fetch_tikwm_info", new=mock.AsyncMock(return_value=info)))
            stack.enter_context(mock.patch("downloader._http_download_stream", new=mock.AsyncMock(side_effect=fake_stream)))
            stack.enter_context(mock.patch("downloader._probe_stream_codec", side_effect=fake_probe))
            if render_ok:
                stack.enter_context(mock.patch("downloader._make_photo_video", side_effect=fake_render))
            result = await _download_via_tikwm(
                "https://www.tiktok.com/@user/photo/123456",
                DOWNLOAD_DIR,
            )
            for path in result.paths + ([result.audio] if result.audio else []):
                self.addCleanup(lambda value=path: Path(value).unlink(missing_ok=True))
            return result

    def test_photo_post_slideshow_video(self):
        """Photo post dạng slideshow (`play` là video thật) → trả VIDEO gốc."""
        result = asyncio_run(self._run(["https://example.com/1.jpg"], video_codec="h264"))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "video")
        self.assertEqual(len(result.paths), 1)
        self.assertIsNone(result.audio)

    def test_photo_post_renders_video(self):
        """Photo post ảnh + nhạc → render slideshow VIDEO (như post TikTok)."""
        result = asyncio_run(self._run(["https://example.com/1.jpg"], render_ok=True))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "video")
        self.assertEqual(len(result.paths), 1)

    def test_photo_post_render_fallback(self):
        """Render fail → fallback gửi album ảnh + audio."""
        result = asyncio_run(self._run(["https://example.com/1.jpg"], render_ok=False))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "photos")
        self.assertIsNotNone(result.audio)

    def test_photo_post_single(self):
        result = asyncio_run(self._run(["https://example.com/1.jpg"]))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "photos")
        self.assertEqual(len(result.paths), 1)
        self.assertEqual(result.title, "Photo album test")
        self.assertTrue(Path(result.paths[0]).exists())

    def test_photo_post_with_music(self):
        """Photo post ảnh tĩnh + nhạc → trả kèm file audio."""
        result = asyncio_run(self._run(["https://example.com/1.jpg"]))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "photos")
        self.assertIsNotNone(result.audio)
        self.assertTrue(Path(result.audio).exists())
        self.assertIn("audio", Path(result.audio).name)

    def test_photo_post_multiple(self):
        result = asyncio_run(self._run([
            "https://example.com/1.jpg",
            "https://example.com/2.jpg",
            "https://example.com/3.jpg",
        ]))
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "photos")
        self.assertEqual(len(result.paths), 3)
        self.assertEqual(result.duration, 3)
        for p in result.paths:
            self.assertTrue(Path(p).exists())

    def test_media_result_kind(self):
        self.assertEqual(MediaResult("video", ["a.mp4"], "T", 5).kind, "video")
        self.assertEqual(MediaResult("photos", ["a.jpg"], "T", 1).paths, ["a.jpg"])
        self.assertIsNone(MediaResult("photos", ["a.jpg"]).audio)


class TestMessageUrlExtraction(unittest.TestCase):
    def test_extracts_raw_and_bare_urls(self):
        raw = SimpleNamespace(
            text="watch https://www.tiktok.com/@user/video/123).",
            caption=None, entities=None, caption_entities=None,
        )
        bare = SimpleNamespace(
            text="www.youtube.com/watch?v=abcdefghijk",
            caption=None, entities=None, caption_entities=None,
        )
        self.assertEqual(
            _extract_url_from_message(raw),
            "https://www.tiktok.com/@user/video/123",
        )
        self.assertEqual(
            _extract_url_from_message(bare),
            "https://www.youtube.com/watch?v=abcdefghijk",
        )

    def test_extracts_text_link_and_caption(self):
        linked = SimpleNamespace(
            text="open this",
            caption=None,
            entities=[SimpleNamespace(type="text_link", url="https://youtu.be/abcdefghijk")],
            caption_entities=None,
        )
        caption = SimpleNamespace(
            text=None,
            caption="https://example.com/video",
            entities=None,
            caption_entities=None,
        )
        self.assertEqual(
            _extract_url_from_message(linked),
            "https://youtu.be/abcdefghijk",
        )
        self.assertEqual(
            _extract_url_from_message(caption),
            "https://example.com/video",
        )


class TestUrlSecurity(unittest.TestCase):
    def test_rejects_private_and_non_http_urls(self):
        for value in (
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
            "https://user:password@example.com/x",
            "https://example.com:8080/x",
        ):
            with self.assertRaises(InvalidURL):
                validate_public_url(value, resolve_dns=False)

    def test_accepts_public_url_without_dns_in_tests(self):
        value = "https://example.com/video?id=secret"
        self.assertEqual(validate_public_url(value, resolve_dns=False), value)
        self.assertEqual(redact_url(value), "https://example.com/video")

    def test_host_suffix_matching_is_exact(self):
        from security import host_matches

        self.assertTrue(host_matches("www.youtube.com", ("youtube.com",)))
        self.assertFalse(host_matches("youtube.com.evil.test", ("youtube.com",)))


class TestPlatformDetection(unittest.TestCase):
    """Nhận diện link theo nền tảng (TikTok / Douyin / YouTube)."""

    def test_tiktok_urls(self):
        for u in [
            "https://vt.tiktok.com/ZSqLpEjoM/",
            "https://vm.tiktok.com/abc/",
            "https://www.tiktok.com/@user/video/123456",
            "https://www.tiktok.com/@user/photo/123456",
        ]:
            self.assertTrue(_is_tiktok_url(u), u)

    def test_douyin_urls(self):
        for u in [
            "https://v.douyin.com/iYRAPA2L/",
            "https://www.douyin.com/video/7624728488368732900",
            "https://www.iesdouyin.com/share/video/7624728488368732900/",
        ]:
            self.assertTrue(_is_douyin_url(u), u)
        self.assertFalse(_is_douyin_url("https://www.tiktok.com/@u/video/1"))

    def test_load_cookies_sets_cookiefile(self):
        opts = {}
        temporary_files = []
        with mock.patch.dict(os.environ, {"TIKTOK_COOKIES": "# Netscape\nx.com\tTRUE\t/"},
                             clear=False):
            _load_cookies(opts, "TIKTOK_COOKIES", "TikTok", temporary_files)
        self.assertIn("cookiefile", opts)
        cookie_path = Path(opts["cookiefile"])
        self.assertTrue(cookie_path.exists())
        self.assertEqual(temporary_files, [cookie_path])
        cookie_path.unlink()

    def test_load_cookies_empty(self):
        opts = {}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOUTUBE_COOKIES", None)
            _load_cookies(opts, "YOUTUBE_COOKIES", "YouTube")
        self.assertNotIn("cookiefile", opts)


class TestYouTubeBlockDetection(unittest.TestCase):
    """Nhận diện thông báo YouTube chặn IP."""

    def test_block_signals(self):
        self.assertTrue(_is_youtube_blocked(
            "ERROR: [youtube] xxx: Failed to extract any player response"
        ))
        self.assertTrue(_is_youtube_blocked(
            "Sign in to confirm you're not a bot"
        ))
        self.assertTrue(_is_youtube_blocked(
            "The page needs to be reloaded"
        ))
        # Lỗi download 403 KHÔNG phải block extraction → vẫn nên thử attempt 2
        self.assertFalse(_is_youtube_blocked(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        ))
        self.assertFalse(_is_youtube_blocked(""))


class TestPipedSelection(unittest.TestCase):
    def test_prefers_highest_quality_that_fits(self):
        streams = [
            {"videoOnly": False, "format": "MPEG_4", "quality": "1080p", "size": MAX_FILE_SIZE + 1},
            {"videoOnly": False, "format": "MPEG_4", "quality": "720p", "size": 1024},
            {"videoOnly": False, "format": "MPEG_4", "quality": "360p", "size": 512},
        ]
        selected, merge = _select_piped_streams(streams, [], 10)
        self.assertEqual(selected["quality"], "720p")
        self.assertFalse(merge)

    def test_returns_none_without_mp4(self):
        selected, merge = _select_piped_streams([], [], 10)
        self.assertIsNone(selected)
        self.assertFalse(merge)


class TestYtdlpOptsAttempts(unittest.TestCase):
    """attempt=0 dùng player clients giới hạn; attempt=1 dùng client mặc định."""

    def test_attempt_variants(self):
        import tempfile
        from downloader import _build_ytdlp_opts
        base = "https://www.youtube.com/watch?v=abc123xyz99"
        opts0 = _build_ytdlp_opts(base, Path(tempfile.gettempdir()), "uid", attempt=0)
        opts1 = _build_ytdlp_opts(base, Path(tempfile.gettempdir()), "uid", attempt=1)
        # attempt 0: danh sách player_client giới hạn (tránh PO token)
        self.assertIn("player_client", opts0["extractor_args"]["youtube"])
        self.assertIsInstance(opts0["extractor_args"]["youtube"]["player_client"], list)
        # attempt 1: fallback default,-web (kỹ thuật VidBee cho IP datacenter)
        self.assertEqual(opts1["extractor_args"]["youtube"]["player_client"], "default,-web")
        # cả 2 vẫn ưu tiên H.264 + AAC
        self.assertIn("vcodec^=avc1", opts0["format"])
        self.assertIn("vcodec^=avc1", opts1["format"])
        # nền tảng khác không có extractor_args
        opts_gen = _build_ytdlp_opts("https://example.com/x", Path(tempfile.gettempdir()), "uid")
        self.assertNotIn("extractor_args", opts_gen)


class TestRunExecutor(unittest.TestCase):
    """Executor chạy nền: heartbeat progress + timeout — không treo vô hạn."""

    def test_heartbeat_and_timeout(self):
        import time as _time
        from downloader import _run_executor

        calls = []

        def slow_func(*args):
            _time.sleep(2.0)
            return "done"

        async def scenario():
            loop = asyncio.get_running_loop()
            t0 = _time.monotonic()
            with self.assertRaises(asyncio.TimeoutError):
                await _run_executor(
                    loop, slow_func, (),
                    progress_cb=lambda p, t: calls.append(t),
                    timeout=0.5,
                    heartbeat_interval=0.15,
                )
            return _time.monotonic() - t0

        elapsed = asyncio_run(scenario())
        self.assertGreater(elapsed, 0.4)
        self.assertTrue(any("Đang xử lý" in c for c in calls), f"thiếu start text: {calls}")
        self.assertTrue(any("xin chờ" in c for c in calls), f"thiếu heartbeat: {calls}")


if __name__ == "__main__":
    unittest.main()

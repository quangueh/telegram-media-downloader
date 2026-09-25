"""Router tải media đa nền tảng với SSRF protection, yt-dlp, API fallback và FFmpeg."""

import os
import re
import base64
import binascii
import json
import time
import uuid
import asyncio
import tempfile
import subprocess
import threading
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Optional, Union
from urllib.parse import urljoin

import httpx
import yt_dlp

from config import (
    MAX_FILE_SIZE,
    MAX_PHOTO_FILE_SIZE,
    MAX_AUDIO_FILE_SIZE,
    MAX_PHOTO_COUNT,
    MAX_ALBUM_DURATION,
    MAX_VIDEO_DURATION,
    URL_VALIDATION_TIMEOUT,
    DOWNLOAD_DIR,
    ALLOW_PRIVATE_URLS,
    logger,
)
from security import InvalidURL, redact_url, validate_public_url


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

TIKTOK_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+", re.IGNORECASE
)
# Douyin (TikTok Trung Quốc): v.douyin.com / www.douyin.com / iesdouyin.com
DOUYIN_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|v\.|ies\.)?(?:douyin\.com|iesdouyin\.com)/[^\s]+",
    re.IGNORECASE,
)
YOUTUBE_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[^\s]+",
    re.IGNORECASE,
)
YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
)

# Player client YouTube không yêu cầu PO Token (thứ tự ưu tiên theo test 2026-09-05)
YOUTUBE_PLAYER_CLIENTS = ["visionos", "android_vr", "android", "tv_embedded", "android_music", "mweb_safari"]
YOUTUBE_POT_PLAYER_CLIENTS = ["mweb", "tv", "web_safari"]

# Attempt 2: bỏ client `web` (bị PO-token chặn hay 403), giữ các client còn lại
# (kỹ thuật từ VidBee — hoạt động tốt trên IP datacenter)
YOUTUBE_CLIENTS_FALLBACK = "default,-web"

# Piped API instances (fallback, kiểm tra theo thứ tự — nhiều instance hay chết, thử nhiều)
PIPED_INSTANCES = [
    "api.piped.projectsegfau.lt",
    "pipedapi.kavin.rocks",
    "pipedapi.adminforge.de",
    "pipedapi.reallyaweso.me",
    "pipedapi.ducks.party",
    "api.piped.private.coffee",
    "pipedapi.moomoo.me",
    "piped-api.lunar.icu",
    "pipedapi.leptons.xyz",
]

TIKTOK_FALLBACK_API = "https://www.tikwm.com/api/"

# Kiểu callback tiến trình: callable(pct_or_None, text) — khai báo kiểu lỏng để tránh circular import
ProgressCB = Optional[object]


# ═══════════════════════════════════════════════════════════════════════════════
#  EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════════════════

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
    """Lỗi khi không thể tải hoặc xử lý video."""
    pass


class MediaFileTooLargeError(VideoTooLargeError):
    def __init__(self, size_bytes: int, max_bytes: int, media_type: str = "Tệp media"):
        super().__init__(size_bytes, max_bytes)
        self.media_type = media_type
        self.message = (
            f"{media_type} ({self.size_mb:.1f}MB) vượt quá giới hạn "
            f"cho phép ({self.max_mb:.0f}MB)."
        )

    def __str__(self) -> str:
        return self.message


@dataclass
class MediaResult:
    kind: str
    paths: list
    title: str = "Media"
    duration: int = 0
    audio: Optional[str] = None
    cleanup_dir: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _url_candidate(url: str) -> str:
    match = re.search(r"https?://[^\s]+", url, re.IGNORECASE)
    return match.group(0).rstrip(".,!?;:]}\"") if match else ""


def _url_host(url: str) -> str:
    from security import get_url_host

    try:
        return get_url_host(_url_candidate(url) or url)
    except InvalidURL:
        return ""


def _is_tiktok_url(url: str) -> bool:
    from security import host_matches

    return host_matches(_url_host(url), ("tiktok.com",))


def _is_douyin_url(url: str) -> bool:
    from security import host_matches

    return host_matches(_url_host(url), ("douyin.com", "iesdouyin.com"))


def _load_cookies(
    opts: dict,
    env_name: str,
    label: str,
    temporary_files: Optional[list[Path]] = None,
) -> None:
    cookies_str = os.getenv(env_name, "").strip()
    source_env = env_name
    encoded_env = f"{env_name}_B64"
    if not cookies_str:
        encoded = os.getenv(encoded_env, "").strip()
        if encoded:
            try:
                decoded = base64.b64decode(encoded, validate=True)
                if len(decoded) > 2 * 1024 * 1024:
                    raise ValueError(f"{encoded_env} vượt quá giới hạn kích thước")
                cookies_str = decoded.decode("utf-8")
                source_env = encoded_env
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise ValueError(
                    f"{encoded_env} không hợp lệ; cần base64 của cookies.txt"
                ) from exc
    if not cookies_str:
        return
    if os.path.isfile(cookies_str):
        cookie_path = Path(cookies_str)
    else:
        if len(cookies_str) > 2 * 1024 * 1024:
            raise ValueError(f"{env_name} vượt quá giới hạn kích thước")
        fd, cookie_name = tempfile.mkstemp(
            prefix=f"{env_name.lower()}_", suffix=".txt",
        )
        os.close(fd)
        cookie_path = Path(cookie_name)
        cookie_path.write_text(cookies_str, encoding="utf-8")
        if temporary_files is not None:
            temporary_files.append(cookie_path)
    try:
        cookie_path.chmod(0o600)
    except OSError:
        pass
    opts["cookiefile"] = str(cookie_path)
    logger.info(f"{label} cookies loaded từ {source_env}.")


def _is_youtube_url(url: str) -> bool:
    from security import host_matches

    return host_matches(_url_host(url), ("youtube.com", "youtu.be"))


def _extract_youtube_id(url: str) -> Optional[str]:
    from urllib.parse import parse_qs, urlsplit

    candidate = _url_candidate(url) or url
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in ("youtu.be", "www.youtu.be"):
            path_id = parsed.path.strip("/").split("/", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", path_id):
                return path_id
        if host == "youtube.com" or host.endswith(".youtube.com"):
            if parsed.path == "/watch":
                values = parse_qs(parsed.query).get("v", [])
                if values and re.fullmatch(r"[A-Za-z0-9_-]{11}", values[0]):
                    return values[0]
            match = re.match(r"^/(?:shorts|embed|v)/([A-Za-z0-9_-]{11})(?:/|$)", parsed.path)
            if match:
                return match.group(1)
    except ValueError:
        return None
    return None


def _safe_filename(title: str, unique_id: str, max_len: int = 50) -> str:
    """Tạo tên file an toàn từ tiêu đề video."""
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", title)[:max_len].strip()
    return safe or "video"


def _cleanup_leftovers(directory: Path, unique_id: str) -> None:
    try:
        for f in directory.glob(f"*{unique_id}*"):
            if f.is_file():
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass


def _remove_path(path: Optional[Union[str, Path]]) -> None:
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


def validate_media_url(url: str) -> str:
    return validate_public_url(
        url,
        resolve_dns=not ALLOW_PRIVATE_URLS,
        allow_private=ALLOW_PRIVATE_URLS,
    )


def _ensure_file_size(
    path: Union[str, Path],
    max_bytes: int = MAX_FILE_SIZE,
    media_type: str = "Tệp media",
) -> int:
    target = Path(path)
    if not target.is_file():
        raise VideoDownloadError("Tệp media sau khi xử lý không tồn tại.")
    size = target.stat().st_size
    if size > max_bytes:
        _remove_path(target)
        raise MediaFileTooLargeError(size, max_bytes, media_type)
    return size


def _new_job_dir(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="job_", dir=str(parent)))


def cleanup_stale_jobs(parent: Path = DOWNLOAD_DIR, max_age: int = 86400) -> None:
    try:
        now = time.time()
        for directory in parent.glob("job_*"):
            if directory.is_dir() and now - directory.stat().st_mtime > max_age:
                _remove_path(directory)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  COMPATIBILITY — đảm bảo file phát được trên mọi thiết bị
# ═══════════════════════════════════════════════════════════════════════════════

# Container MP4 hợp lệ (theo ffprobe format_name)
_MP4_CONTAINERS = ("mov", "mp4", "m4a", "3gp", "3g2", "mj2")


def _probe_stream_codec(file_path: str, stream: str) -> str:
    """Trả về codec của stream (v:0 hoặc a:0), '' nếu stream không tồn tại."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _probe_container(file_path: str) -> str:
    """Trả về tên container (format_name) theo ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _is_mp4_container(container: str) -> bool:
    """Kiểm tra container có phải họ MP4 không (format_name có thể là list phân tách phẩy)."""
    if not container:
        return False
    tokens = [t.strip().lower() for t in container.split(",") if t.strip()]
    return any(t in _MP4_CONTAINERS for t in tokens)


def _probe_duration(file_path: str) -> float:
    """Trả về thời lượng video (giây), 0 nếu không xác định."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip() or 0)
    except Exception:
        return 0.0


def _kill_process(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ensure_playable(file_path: str, progress_cb: ProgressCB = None) -> str:
    vcodec = _probe_stream_codec(file_path, "v:0")
    acodec = _probe_stream_codec(file_path, "a:0")
    container = _probe_container(file_path)
    if (
        vcodec in ("h264", "avc1")
        and (not acodec or acodec in ("aac", "mp3"))
        and _is_mp4_container(container)
    ):
        return file_path

    duration = _probe_duration(file_path)
    tmp_path = file_path + ".playable.mp4"
    cmd = ["ffmpeg", "-y", "-i", file_path]
    if vcodec in ("h264", "avc1"):
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if acodec and acodec not in ("aac", "mp3"):
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [
        "-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1",
        "-nostats", "-loglevel", "error", tmp_path,
    ]

    def report(pct, text):
        if progress_cb:
            try:
                progress_cb(pct, text)
            except Exception:
                pass

    report(None, "🔧 Đang chuyển codec sang H.264 + AAC...")
    proc = None
    timer = None
    output_tail: list[str] = []
    last_report = 0.0
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        timer = threading.Timer(900, _kill_process, args=(proc,))
        timer.daemon = True
        timer.start()
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if line:
                output_tail.append(line)
                if len(output_tail) > 30:
                    output_tail.pop(0)
            if line.startswith("out_time_us="):
                try:
                    out_us = int(line.split("=", 1)[1])
                    if duration > 0 and out_us > 0:
                        pct = min(99.0, out_us / (duration * 1_000_000) * 100)
                        now = time.monotonic()
                        if now - last_report >= 1.0:
                            last_report = now
                            report(pct, f"🔧 Đang chuyển codec... {pct:.0f}%")
                except ValueError:
                    pass
        proc.wait()
        if proc.returncode == 0 and os.path.exists(tmp_path):
            os.replace(tmp_path, file_path)
            _ensure_file_size(file_path, MAX_FILE_SIZE, "Video")
            report(100, "✅ Chuyển codec xong (H.264 + AAC)")
            return file_path
        logger.warning("Re-encode thất bại: %s", " ".join(output_tail)[-300:])
    except Exception as exc:
        logger.warning("Re-encode lỗi: %s", exc)
    finally:
        if timer:
            timer.cancel()
        if proc:
            if proc.poll() is None:
                _kill_process(proc)
            try:
                proc.stdout.close()
            except Exception:
                pass
        _remove_path(tmp_path)
    return file_path


# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED: HTTP STREAM DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

# Headers giả trình duyệt — nhiều CDN (TikTok...) chặn/throttle request không có UA hợp lệ
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
    "Accept": "video/webm,video/mp4,video/*,*/*;q=0.8",
}


class IncompleteDownloadError(DownloaderError):
    """File tải về bị cắt ngắn (ít hơn content-length) — file hỏng, cần tải lại."""


async def _read_limited_json(
    response: httpx.Response,
    max_bytes: int = 2 * 1024 * 1024,
) -> dict:
    length = int(response.headers.get("content-length") or 0)
    if length > max_bytes:
        raise IncompleteDownloadError("Dữ liệu API vượt quá giới hạn.")
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=1 << 16):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise IncompleteDownloadError("Dữ liệu API vượt quá giới hạn.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncompleteDownloadError("API trả về JSON không hợp lệ.") from exc


async def _http_download_stream(
    url: str,
    dest: Path,
    timeout: int = 60,
    progress_cb: ProgressCB = None,
    label: str = "⬇️ Đang tải",
    headers: Optional[dict] = None,
    progress_interval: float = 1.0,
    max_bytes: int = MAX_FILE_SIZE,
    media_type: str = "Tệp media",
) -> int:
    downloaded = 0
    last_report = 0.0
    total = 0
    current_url = url
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15),
        follow_redirects=False,
        headers=headers or _BROWSER_HEADERS,
    ) as client:
        for redirect_count in range(6):
            if ALLOW_PRIVATE_URLS:
                await asyncio.to_thread(validate_public_url, current_url, resolve_dns=False, allow_private=True)
            else:
                await asyncio.to_thread(validate_public_url, current_url)
            async with client.stream("GET", current_url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location or redirect_count >= 5:
                        raise IncompleteDownloadError("Chuyển hướng URL không hợp lệ.")
                    current_url = urljoin(str(resp.url), location)
                    continue
                resp.raise_for_status()
                try:
                    total = int(resp.headers.get("content-length") or 0)
                except ValueError:
                    total = 0
                if total > max_bytes:
                    raise MediaFileTooLargeError(total, max_bytes, media_type)
                with open(dest, "wb") as file:
                    async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise MediaFileTooLargeError(downloaded, max_bytes, media_type)
                        file.write(chunk)
                        if progress_cb:
                            now = time.monotonic()
                            if now - last_report >= progress_interval:
                                last_report = now
                                if total > 0:
                                    pct = downloaded / total * 100
                                    text = f"{label}: {_fmt_mb(downloaded)}/{_fmt_mb(total)}"
                                else:
                                    pct = None
                                    text = f"{label}: {_fmt_mb(downloaded)}"
                                try:
                                    progress_cb(pct, text)
                                except Exception:
                                    pass
                break
        else:
            raise IncompleteDownloadError("Chuyển hướng URL quá nhiều lần.")

    if total > 0 and downloaded < total:
        raise IncompleteDownloadError(
            f"Tải xuống bị cắt ngắn: {downloaded}/{total} bytes"
        )
    return downloaded


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKEND 1: yt-dlp (bgutil + player clients + cookies)
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_mb(num_bytes: float) -> str:
    """Format bytes → MB string."""
    return f"{num_bytes / (1024 * 1024):.1f}MB"


# Strip ANSI escape codes (yt-dlp error dính màu terminal: [0;31mERROR[0m)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(e: BaseException) -> str:
    """Làm sạch thông điệp lỗi: bỏ ANSI codes, ẩn URL nhạy cảm, giới hạn độ dài."""
    msg = _ANSI_RE.sub("", str(e)).strip()
    msg = re.sub(
        r"https?://[^\s<>]+",
        lambda match: redact_url(match.group(0)),
        msg,
    )
    if len(msg) > 280:
        msg = msg[:280] + "..."
    return msg


# Dấu hiệu IP máy chủ bị YouTube chặn (không phải lỗi tạm thời của từng client)
_YOUTUBE_BLOCK_SIGNALS = (
    "failed to extract any player response",
    "sign in to confirm you're not a bot",
    "unable to extract initial data",
    "incomplete data received from youtube",
    "reload the page",
    "the page needs to be reloaded",
)


def _is_youtube_blocked(msg: str) -> bool:
    """Nhận diện thông báo YouTube chặn IP (player response / bot-check)."""
    low = msg.lower()
    return any(s in low for s in _YOUTUBE_BLOCK_SIGNALS)


def _build_ytdlp_opts(
    url: str,
    target_dir: Path,
    unique_id: str,
    progress_cb: ProgressCB = None,
    attempt: int = 0,
    cookie_files: Optional[list[Path]] = None,
) -> dict:
    """Xây dựng ydl_opts cho yt-dlp, tối ưu theo nền tảng.

    attempt=0: giới hạn player clients đã biết (tránh PO token).
    attempt=1: client mặc định của yt-dlp.
    attempt=2: android_vr fallback cho IP bị chặn.
    """
    outtmpl = str(target_dir / f"media_{unique_id}.%(ext)s")

    # Ưu tiên H.264 + AAC trong mp4 — tương thích mọi thiết bị / Telegram.
    # Tránh các format "enhanced" mới của YouTube (VP9/AV1/HEVC nhét trong mp4,
    # VD: format 395/616...) vì gây màn trắng trên điện thoại / unsupported trên PC.
    opts = {
        "format": (
            "best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "noplaylist": True,
        "postprocessor_args": ["-movflags", "+faststart"],
        # Fast-fail: tránh yt-dlp retry mặc định 10 lần → treo hàng phút khi bị chặn
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 1,
        "socket_timeout": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        },
    }

    if _is_youtube_url(url):
        youtube_proxy = os.getenv("YOUTUBE_PROXY", "").strip()
        if youtube_proxy:
            opts["proxy"] = youtube_proxy

    # YouTube: ưu tiên format H.264 + AAC <45MB (giống nhau ở mọi attempt)
    if _is_youtube_url(url):
        opts["format"] = (
            "best[ext=mp4][vcodec^=avc1][acodec^=mp4a][filesize_approx<45M]/"
            "bestvideo[ext=mp4][vcodec^=avc1][filesize_approx<45M]+bestaudio[ext=m4a]/"
            "best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        )
        if attempt == 0:
            player_clients = (
                YOUTUBE_POT_PLAYER_CLIENTS
                if os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip()
                else YOUTUBE_PLAYER_CLIENTS
            )
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": player_clients,
                    "player_skip": ["js"],
                }
            }
        elif attempt == 1:
            logger.info("YouTube: dùng player clients default,-web (attempt 2)")
            opts["extractor_args"] = {
                "youtube": {"player_client": YOUTUBE_CLIENTS_FALLBACK}
            }
        else:
            logger.info("YouTube: dùng player client android_vr (attempt 3)")
            opts["extractor_args"] = {
                "youtube": {"player_client": "android_vr"}
            }

    if _is_youtube_url(url):
        provider_url = os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip()
        if provider_url:
            opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
                "base_url": provider_url,
            }

    # Cookies theo platform (giúp tải từ IP datacenter bị chặn)
    if _is_youtube_url(url):
        _load_cookies(opts, "YOUTUBE_COOKIES", "YouTube", cookie_files)
    if _is_tiktok_url(url) or _is_douyin_url(url):
        _load_cookies(opts, "TIKTOK_COOKIES", "TikTok/Douyin", cookie_files)

    # Progress hook: report % tải về qua callback (nếu có)
    if progress_cb:
        def _ytdlp_progress_hook(d: dict) -> None:
            try:
                status = d.get("status")
                if status == "downloading":
                    downloaded = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    speed = d.get("speed") or 0
                    speed_str = f" — {_fmt_mb(speed)}/s" if speed else ""
                    if total > 0:
                        pct = downloaded / total * 100
                        text = f"⬇️ Đang tải: {_fmt_mb(downloaded)}/{_fmt_mb(total)}{speed_str}"
                    else:
                        pct = None
                        text = f"⬇️ Đang tải: {_fmt_mb(downloaded)}{speed_str}"
                    progress_cb(pct, text)
                elif status == "finished":
                    progress_cb(None, "🔧 Đang gộp video + audio (FFmpeg)...")
            except Exception:
                pass  # Progress lỗi không được làm hỏng download
        opts["progress_hooks"] = [_ytdlp_progress_hook]

    return opts


def _sync_ytdlp_download(
    url: str,
    output_dir: Union[str, Path],
    progress_cb: ProgressCB = None,
) -> Tuple[str, str, int]:
    """
    Backend chính: yt-dlp sync (chạy trong executor).
    Tự phát hiện bgutil PO-token server, player clients bypass, cookies.
    """
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    unique_id = uuid.uuid4().hex[:10]

    logger.info("yt-dlp đang xử lý: %s", redact_url(url))

    # YouTube thử tối đa 3 player client trước khi chuyển backend khác.
    for attempt in range(3):
        cookie_files: list[Path] = []
        opts = _build_ytdlp_opts(
            url, target_dir, unique_id, progress_cb, attempt=attempt,
            cookie_files=cookie_files,
        )
        downloaded_filepath: Optional[str] = None

        def postprocessor_hook(info: dict) -> None:
            nonlocal downloaded_filepath
            if info.get("status") == "finished":
                downloaded_filepath = (
                    info.get("info_dict", {}).get("_filename") or info.get("filepath")
                )

        opts["postprocessor_hooks"] = [postprocessor_hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info_dict = ydl.extract_info(url, download=False)
                if not info_dict:
                    raise VideoDownloadError("Không thể lấy thông tin từ URL được cung cấp.")

                # Kiểm tra kích thước ước lượng nếu có sẵn
                estimated_size = info_dict.get("filesize") or info_dict.get("filesize_approx")
                try:
                    estimated_size = int(estimated_size or 0)
                except (TypeError, ValueError):
                    estimated_size = 0
                if estimated_size > MAX_FILE_SIZE:
                    raise VideoTooLargeError(estimated_size, MAX_FILE_SIZE)

                title = str(info_dict.get("title", "Video") or "Video")
                try:
                    duration = int(info_dict.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                if duration > MAX_VIDEO_DURATION:
                    raise VideoDownloadError("Video vượt quá thời lượng được phép.")

                download_info = ydl.extract_info(url, download=True)

                final_file = downloaded_filepath or ydl.prepare_filename(download_info)
                if not os.path.exists(final_file):
                    base = os.path.splitext(final_file)[0]
                    for ext in [".mp4", ".mkv", ".webm"]:
                        if os.path.exists(base + ext):
                            final_file = base + ext
                            break
                    else:
                        candidates = [
                            f for f in target_dir.glob(f"*{unique_id}*")
                            if not f.name.endswith((".part", ".ytdl", ".temp"))
                        ]
                        if candidates:
                            final_file = str(candidates[0])
                        else:
                            raise VideoDownloadError("Không tìm thấy tệp video sau khi tải xuống.")

                _ensure_file_size(final_file, MAX_FILE_SIZE, "Video")
                final_file = _ensure_playable(final_file, progress_cb=progress_cb)
                actual_size = _ensure_file_size(final_file, MAX_FILE_SIZE, "Video")

                logger.info(
                    "yt-dlp tải thành công: %s (%.1fMB)",
                    Path(final_file).name, actual_size / (1024 * 1024),
                )
                return final_file, title, duration
        except yt_dlp.utils.DownloadError as e:
            msg = _clean_error(e)
            is_youtube = _is_youtube_url(url)
            cookies_set = bool(
                os.getenv("YOUTUBE_COOKIES", "").strip()
                or os.getenv("YOUTUBE_COOKIES_B64", "").strip()
            )
            if is_youtube and attempt < 2:
                _cleanup_leftovers(target_dir, unique_id)
                if _is_youtube_blocked(msg) and not cookies_set:
                    logger.warning(
                        "YouTube attempt %s bị chặn: %s — thử client khác",
                        attempt + 1, msg[:160],
                    )
                else:
                    logger.warning(
                        "yt-dlp attempt %s fail: %s — thử client khác",
                        attempt + 1, msg[:160],
                    )
                continue
            if is_youtube and _is_youtube_blocked(msg) and not cookies_set:
                raise VideoDownloadError(
                    "YouTube bị chặn IP máy chủ sau khi thử các player client.\n"
                    "Hãy cấu hình YOUTUBE_COOKIES (cookie Netscape từ trình duyệt "
                    "đã đăng nhập) hoặc thử lại sau."
                ) from e
            raise VideoDownloadError(msg) from e
        finally:
            for cookie_path in cookie_files:
                _remove_path(cookie_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKEND 2: Piped API (YouTube proxy, fallback khi yt-dlp bị chặn)
# ═══════════════════════════════════════════════════════════════════════════════

def _piped_height(stream: dict) -> int:
    match = re.search(r"(\d+)p", str(stream.get("quality", "")))
    return int(match.group(1)) if match else 0


def _piped_estimated_size(stream: dict, duration: int) -> Optional[int]:
    size = stream.get("size") or stream.get("filesize")
    try:
        if size:
            return int(size)
    except (TypeError, ValueError):
        pass
    bitrate = stream.get("bitrate") or stream.get("averageBitrate")
    try:
        if bitrate and duration:
            return int(float(bitrate) * max(1, duration) / 8)
    except (TypeError, ValueError):
        pass
    return None


def _select_piped_streams(
    streams: list,
    audio_streams: list,
    duration: int,
) -> tuple[Optional[dict], bool]:
    combined = [
        stream for stream in streams
        if not stream.get("videoOnly") and stream.get("format") == "MPEG_4"
    ]
    video_only = [
        stream for stream in streams
        if stream.get("videoOnly") and stream.get("format") == "MPEG_4"
    ]
    combined.sort(
        key=lambda stream: (
            _piped_height(stream),
            stream.get("bitrate") or stream.get("averageBitrate") or 0,
        ),
        reverse=True,
    )
    for stream in combined:
        estimate = _piped_estimated_size(stream, duration)
        if estimate is None or estimate <= MAX_FILE_SIZE:
            return stream, False
    video_only.sort(
        key=lambda stream: (
            _piped_height(stream),
            stream.get("bitrate") or stream.get("averageBitrate") or 0,
        ),
        reverse=True,
    )
    for stream in video_only:
        estimate = _piped_estimated_size(stream, duration)
        if estimate is None or estimate <= MAX_FILE_SIZE:
            return stream, bool(audio_streams)
    return None, False


async def _download_via_piped(
    url: str,
    output_dir: Path,
    progress_cb: ProgressCB = None,
) -> Optional[Tuple[str, str, int]]:
    """Tải YouTube video qua Piped API — proxy stream không bị bot-check."""
    video_id = _extract_youtube_id(url)
    if not video_id:
        return None

    target_dir = output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    unique_id = uuid.uuid4().hex[:10]

    for instance in PIPED_INSTANCES:
        api_url = f"https://{instance}/streams/{video_id}"
        try:
            if ALLOW_PRIVATE_URLS:
                await asyncio.to_thread(validate_public_url, api_url, resolve_dns=False, allow_private=True)
            else:
                await asyncio.to_thread(validate_public_url, api_url)
            async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                current_api_url = api_url
                for redirect_count in range(4):
                    async with client.stream("GET", current_api_url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or redirect_count >= 3:
                                raise IncompleteDownloadError("Piped chuyển hướng không hợp lệ.")
                            current_api_url = urljoin(str(response.url), location)
                            if ALLOW_PRIVATE_URLS:
                                await asyncio.to_thread(
                                    validate_public_url, current_api_url,
                                    resolve_dns=False, allow_private=True,
                                )
                            else:
                                await asyncio.to_thread(validate_public_url, current_api_url)
                            continue
                        response.raise_for_status()
                        data = await _read_limited_json(response)
                        break
                else:
                    raise IncompleteDownloadError("Piped chuyển hướng quá nhiều lần.")
        except Exception as e:
            logger.warning(f"Piped [{instance}] fail: {e}")
            continue

        if data.get("error"):
            logger.warning(f"Piped [{instance}] error: {data.get('error')}")
            continue

        title = str(data.get("title", "YouTube video") or "YouTube video")
        try:
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if duration > MAX_VIDEO_DURATION:
            raise VideoDownloadError("Video vượt quá thời lượng được phép.")

        streams = data.get("videoStreams", []) or []
        audio_streams = data.get("audioStreams", []) or []
        if not isinstance(streams, list) or not isinstance(audio_streams, list):
            continue
        chosen_stream, need_merge = _select_piped_streams(streams, audio_streams, duration)
        if chosen_stream is None:
            logger.warning(f"Piped [{instance}] không tìm thấy mp4 stream phù hợp")
            continue

        stream_url = chosen_stream.get("url")
        if not stream_url:
            continue
        try:
            if ALLOW_PRIVATE_URLS:
                await asyncio.to_thread(validate_public_url, stream_url, resolve_dns=False, allow_private=True)
            else:
                await asyncio.to_thread(validate_public_url, stream_url)
        except InvalidURL:
            continue

        logger.info(
            "Piped [%s] chọn stream %s merge=%s",
            instance, chosen_stream.get("quality"), need_merge,
        )

        # Tải video stream
        safe_title = _safe_filename(title, unique_id)
        video_path = target_dir / f"{safe_title}_{unique_id}_video.mp4"

        try:
            await _http_download_stream(
                stream_url, video_path, progress_cb=progress_cb,
                label="⬇️ Tải video (Piped)", max_bytes=MAX_FILE_SIZE,
                media_type="Video",
            )
        except VideoTooLargeError:
            raise
        except Exception as e:
            _remove_path(video_path)
            logger.warning(f"Piped [{instance}] tải video fail: {e}")
            continue

        if need_merge:
            # Tìm audio stream m4a tốt nhất
            audio_m4a = [a for a in audio_streams if a.get("format") == "M4A"]
            if not audio_m4a:
                audio_m4a = audio_streams
            if not audio_m4a:
                logger.warning("Piped: không tìm thấy audio stream, dùng video-only")
                final_path = str(video_path)
            else:
                audio_m4a.sort(
                    key=lambda stream: stream.get("bitrate") or stream.get("averageBitrate") or 0,
                    reverse=True,
                )
                audio_url = audio_m4a[0].get("url")
                audio_path = target_dir / f"{safe_title}_{unique_id}_audio.m4a"
                try:
                    await _http_download_stream(
                        audio_url, audio_path, progress_cb=progress_cb,
                        label="⬇️ Tải audio (Piped)", max_bytes=MAX_AUDIO_FILE_SIZE,
                        media_type="Audio",
                    )
                except Exception as e:
                    logger.warning(f"Piped [{instance}] tải audio fail: {e}")
                    final_path = str(video_path)
                    await asyncio.sleep(0)
                    return (final_path, title, duration)

                # FFmpeg merge — chạy trong thread tránh block event loop
                final_path = str(target_dir / f"{safe_title}_{unique_id}.mp4")
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
                         "-c", "copy", final_path],
                        capture_output=True, timeout=300,
                    )
                    if result.returncode != 0 or not os.path.exists(final_path):
                        logger.warning(f"Piped FFmpeg merge fail: {result.stderr[-200:]}")
                        final_path = str(video_path)
                except Exception as e:
                    logger.warning(f"Piped FFmpeg error: {e}")
                    final_path = str(video_path)
                finally:
                    for p in [video_path, audio_path]:
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass
        else:
            # Rename progressive mp4
            final_path = str(target_dir / f"{safe_title}_{unique_id}.mp4")
            try:
                video_path.rename(Path(final_path))
            except OSError:
                final_path = str(video_path)

        # Kiểm tra kích thước cuối
        if os.path.exists(final_path):
            _ensure_file_size(final_path, MAX_FILE_SIZE, "Video")
            final_path = await asyncio.to_thread(_ensure_playable, final_path, progress_cb)
            actual_size = _ensure_file_size(final_path, MAX_FILE_SIZE, "Video")
            logger.info(
                "Piped tải thành công: %s (%.1fMB)",
                Path(final_path).name, actual_size / (1024 * 1024),
            )
            return final_path, title, duration

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKEND 3: TikTok tikwm API (ưu tiên cho TikTok — nhanh, không watermark, ổn định hơn yt-dlp trên datacenter IP)
# ═══════════════════════════════════════════════════════════════════════════════

async def _download_stream_with_retry(
    url: str,
    dest: Path,
    label: str,
    progress_cb: ProgressCB = None,
    attempts: int = 3,
    max_bytes: int = MAX_FILE_SIZE,
    media_type: str = "Tệp media",
) -> int:
    """Tải stream với retry — CDN TikTok hay flaky/cắt ngắn, request mới thường thành công."""
    last_err: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            if progress_cb:
                try:
                    progress_cb(None, f"⚠️ {label}: kết nối chậm, thử lại lần {attempt}/{attempts}...")
                except Exception:
                    pass
            await asyncio.sleep(1.0)
        try:
            return await _http_download_stream(
                url, dest, timeout=40, progress_cb=progress_cb, label=label,
                max_bytes=max_bytes, media_type=media_type,
            )
        except VideoTooLargeError:
            raise
        except Exception as e:
            last_err = e
            logger.warning(f"{label} lần {attempt}/{attempts} fail: {_clean_error(e)}")
            if dest.exists():
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
    if last_err:
        raise last_err
    return 0


async def _fetch_tikwm_info(url: str) -> Optional[dict]:
    if not _is_tiktok_url(url):
        return None
    tiktok_url = _url_candidate(url) or url
    if "vm.tiktok.com" in tiktok_url or "vt.tiktok.com" in tiktok_url:
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=15, headers=_BROWSER_HEADERS,
            ) as client:
                current_url = tiktok_url
                for redirect_count in range(4):
                    if ALLOW_PRIVATE_URLS:
                        await asyncio.to_thread(validate_public_url, current_url, resolve_dns=False, allow_private=True)
                    else:
                        await asyncio.to_thread(validate_public_url, current_url)
                    async with client.stream("GET", current_url) as response:
                        if response.status_code not in (301, 302, 303, 307, 308):
                            response.raise_for_status()
                            tiktok_url = str(response.url)
                            break
                        location = response.headers.get("location")
                    if not location or redirect_count >= 3:
                        raise IncompleteDownloadError("TikTok chuyển hướng không hợp lệ.")
                    current_url = urljoin(str(response.url), location)
                else:
                    raise IncompleteDownloadError("TikTok chuyển hướng quá nhiều lần.")
        except Exception as exc:
            logger.warning("Không thể mở rộng liên kết TikTok: %s", _clean_error(exc))

    try:
        if ALLOW_PRIVATE_URLS:
            await asyncio.to_thread(
                validate_public_url, TIKTOK_FALLBACK_API, resolve_dns=False,
                allow_private=True,
            )
        else:
            await asyncio.to_thread(validate_public_url, TIKTOK_FALLBACK_API)
        async with httpx.AsyncClient(timeout=60, headers=_BROWSER_HEADERS) as client:
            async with client.stream(
                "POST", TIKTOK_FALLBACK_API, data={"url": tiktok_url, "hd": "1"},
            ) as resp:
                resp.raise_for_status()
                data = await _read_limited_json(resp)
    except Exception as exc:
        logger.warning("TikTok tikwm API không phản hồi: %s", _clean_error(exc))
        return None

    if data.get("code") != 0 or not data.get("data"):
        logger.warning("TikTok tikwm API lỗi: %s", data.get("msg"))
        return None
    return data["data"]


def _make_photo_video(
    image_paths: list,
    audio_path: str,
    output_path: str,
    duration: float,
) -> str:
    """Render 1/nhiều ảnh + nhạc nền thành video MP4 (H.264 + AAC).

    TikTok photo post chỉ cung cấp ảnh + nhạc từ API (không có file video riêng),
    nên muốn video phát được phải render slideshow bằng ffmpeg.
    """
    try:
        if not image_paths or len(image_paths) > MAX_PHOTO_COUNT:
            raise ValueError("Số lượng ảnh vượt quá giới hạn.")
        duration = max(1.0, min(float(duration), float(MAX_ALBUM_DURATION)))
        if len(image_paths) == 1:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", image_paths[0],
                "-i", audio_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-t", f"{duration:.2f}",
                "-movflags", "+faststart",
                "-loglevel", "error",
                output_path,
            ]
        else:
            seg = max(2.0, duration / len(image_paths))
            inputs = []
            for p in image_paths:
                inputs += ["-loop", "1", "-t", f"{seg:.2f}", "-i", p]
            inputs += ["-i", audio_path]
            filters = [
                (
                    f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}]"
                )
                for i in range(len(image_paths))
            ]
            filters.append(
                "".join(f"[v{i}]" for i in range(len(image_paths)))
                + f"concat=n={len(image_paths)}:v=1:a=0[vout]"
            )
            cmd = [
                "ffmpeg", "-y", *inputs,
                "-filter_complex", ";".join(filters),
                "-map", "[vout]", "-map", f"{len(image_paths)}:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-t", f"{duration:.2f}",
                "-movflags", "+faststart",
                "-loglevel", "error",
                output_path,
            ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not os.path.exists(output_path):
            raise RuntimeError(
                f"FFmpeg slideshow fail: {result.stderr.decode('utf-8', errors='replace')[-200:]}"
            )
        return output_path
    except Exception as e:
        raise RuntimeError(f"FFmpeg slideshow error: {e}")


async def _download_via_tikwm(
    url: str,
    output_dir: Path,
    progress_cb: ProgressCB = None,
) -> Optional[MediaResult]:
    """Tải TikTok qua tikwm API — hỗ trợ cả video lẫn bài đăng ảnh (photo post)."""
    info = await _fetch_tikwm_info(url)
    if not info:
        return None

    title = str(info.get("title") or "Video TikTok").strip() or "Video TikTok"

    target_dir = output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    unique_id = uuid.uuid4().hex[:10]

    # ── TikTok PHOTO POST: có 2 dạng ──
    #  A) `play`/`hdplay` là VIDEO thật (post mixed/đặc biệt) → gửi video gốc
    #  B) Ảnh + nhạc (phổ biến): tikwm chỉ cấp ảnh + MP3 → render slideshow video
    images = info.get("images") or []
    duration = int(info.get("duration") or 0)
    if not isinstance(images, list):
        images = []
    if images and duration > MAX_ALBUM_DURATION:
        raise VideoDownloadError("Album vượt quá thời lượng được phép.")
    if not images and duration > MAX_VIDEO_DURATION:
        raise VideoDownloadError("Video vượt quá thời lượng được phép.")
    if len(images) > MAX_PHOTO_COUNT:
        raise VideoDownloadError("Album vượt quá số lượng ảnh được phép.")
    if images:
        audio_path: Optional[str] = None
        media_url = info.get("play") or info.get("hdplay")
        if media_url:
            media_file = target_dir / f"{unique_id}_media.bin"
            try:
                await _http_download_stream(
                    media_url, media_file, timeout=40, label="⏳ Kiểm tra media...",
                    max_bytes=MAX_FILE_SIZE, media_type="Video",
                )
                vcodec = await asyncio.to_thread(_probe_stream_codec, str(media_file), "v:0")
                if vcodec:
                    # Dạng A: video slideshow sẵn có → gửi video gốc
                    final = str(target_dir / f"{unique_id}.mp4")
                    try:
                        os.replace(media_file, final)
                    except OSError:
                        final = str(media_file)
                    final = await asyncio.to_thread(_ensure_playable, final, progress_cb)
                    logger.info(f"TikTok photo post dạng video: {final}")
                    return MediaResult("video", [final], title=title, duration=duration)
                # Dạng B: `play` là MP3 nhạc nền → dùng làm audio
                audio_file = str(target_dir / f"{unique_id}_audio.mp3")
                try:
                    os.replace(media_file, audio_file)
                except OSError:
                    audio_file = str(media_file)
                audio_path = audio_file
                _ensure_file_size(audio_path, MAX_AUDIO_FILE_SIZE, "Audio")
            except Exception as e:
                logger.warning(f"Kiểm tra media photo post fail: {e}")
                try:
                    media_file.unlink(missing_ok=True)
                except OSError:
                    pass

        # Dạng B chưa có audio (play lỗi) → thử field `music`
        if not audio_path:
            music_url = info.get("music")
            if music_url:
                try:
                    audio_file = target_dir / f"{unique_id}_audio.mp3"
                    await _http_download_stream(
                        music_url, audio_file, timeout=40, label="🎵 Tải nhạc nền",
                        max_bytes=MAX_AUDIO_FILE_SIZE, media_type="Audio",
                    )
                    if not await asyncio.to_thread(_probe_stream_codec, str(audio_file), "v:0"):
                        audio_path = str(audio_file)
                    else:
                        try:
                            audio_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                except Exception as e:
                    logger.warning(f"Tải nhạc nền TikTok photo fail: {e}")

        # Tải các ảnh
        paths: list = []
        try:
            for i, img_url in enumerate(images, 1):
                img_path = target_dir / f"{unique_id}_{i:02d}.jpg"
                await _http_download_stream(
                    img_url, img_path, timeout=40,
                    label=f"🖼️ Tải ảnh {i}/{len(images)}",
                    max_bytes=MAX_PHOTO_FILE_SIZE, media_type="Ảnh",
                )
                paths.append(str(img_path))
        except Exception as e:
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if audio_path:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            _cleanup_leftovers(target_dir, unique_id)
            logger.warning(f"Tải ảnh TikTok photo fail: {e}")
            return None

        # ── Dạng B: có ảnh + nhạc → render slideshow VIDEO (như post TikTok) ──
        if audio_path and paths:
            video_out = str(target_dir / f"{unique_id}.mp4")
            audio_dur = await asyncio.to_thread(_probe_duration, audio_path) or 10.0
            audio_dur = max(1.0, min(audio_dur, float(MAX_ALBUM_DURATION)))
            try:
                # Chạy ffmpeg trong thread — tránh block event loop (bot không bị đơ)
                await asyncio.to_thread(_make_photo_video, paths, audio_path, video_out, audio_dur)
                _ensure_file_size(video_out, MAX_FILE_SIZE, "Video")
                for p in paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
                logger.info(f"TikTok photo post → video: {video_out}")
                return MediaResult("video", [video_out], title=title, duration=int(audio_dur))
            except Exception as e:
                logger.warning(f"Render slideshow fail, gửi ảnh + nhạc riêng: {e}")

        for path in paths:
            _ensure_file_size(path, MAX_PHOTO_FILE_SIZE, "Ảnh")
        if audio_path:
            _ensure_file_size(audio_path, MAX_AUDIO_FILE_SIZE, "Audio")
        logger.info(
            "TikTok photo post: %s ảnh%s", len(paths),
            " + nhạc nền" if audio_path else "",
        )
        return MediaResult("photos", paths, title=title, duration=len(paths), audio=audio_path)

    # ── TikTok VIDEO ──
    estimated_size = info.get("size") or 0
    if isinstance(estimated_size, (int, float)) and estimated_size > MAX_FILE_SIZE:
        raise VideoTooLargeError(int(estimated_size), MAX_FILE_SIZE)

    # Ưu tiên stream `play` (H.264, phát được mọi thiết bị) — `hdplay` thường là
    # HEVC, phải re-encode chậm. Chỉ dùng hdplay nếu không có play.
    video_url = info.get("play") or info.get("hdplay")
    if not video_url:
        return None

    safe_title = _safe_filename(title, unique_id)
    file_path = target_dir / f"{safe_title}_{unique_id}.mp4"

    try:
        await _download_stream_with_retry(
            video_url, file_path, label="⬇️ Tải TikTok (tikwm)", progress_cb=progress_cb,
            max_bytes=MAX_FILE_SIZE, media_type="Video",
        )
    except VideoTooLargeError:
        _cleanup_leftovers(target_dir, unique_id)
        raise
    except Exception as e:
        _cleanup_leftovers(target_dir, unique_id)
        logger.warning(f"Tải TikTok tikwm stream fail: {e}")
        return None

    logger.info("TikTok tikwm tải thành công: %s", Path(file_path).name)
    file_path = await asyncio.to_thread(_ensure_playable, str(file_path), progress_cb)
    _ensure_file_size(file_path, MAX_FILE_SIZE, "Video")
    return MediaResult("video", [str(file_path)], title=title, duration=duration)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTER: extract_and_download()
# ═══════════════════════════════════════════════════════════════════════════════

# Thời gian tối đa cho 1 lần chạy yt-dlp (tránh treo vô hạn khi IP bị chặn)
YTDLP_TIMEOUT = 150


async def _run_executor(
    loop: asyncio.AbstractEventLoop,
    func,
    args: tuple,
    progress_cb: ProgressCB = None,
    timeout: int = YTDLP_TIMEOUT,
    start_text: str = "⏳ Đang xử lý...",
    heartbeat_text: str = "⏳ Vẫn đang xử lý, xin chờ...",
    heartbeat_interval: float = 10.0,
):
    """Chạy func trong executor với heartbeat progress + timeout (tránh 'cứng đơ')."""
    if progress_cb:
        try:
            progress_cb(None, start_text)
        except Exception:
            pass

    executor_future = loop.run_in_executor(None, func, *args)

    heartbeat = None
    if progress_cb:
        async def beat():
            while True:
                try:
                    await asyncio.sleep(heartbeat_interval)
                    progress_cb(None, heartbeat_text)
                except asyncio.CancelledError:
                    return
                except Exception:
                    pass
        heartbeat = asyncio.create_task(beat())

    try:
        return await asyncio.wait_for(executor_future, timeout=timeout)
    except asyncio.TimeoutError:
        def consume_result(future):
            try:
                future.exception()
            except BaseException:
                pass
        executor_future.add_done_callback(consume_result)
        raise
    finally:
        if heartbeat:
            heartbeat.cancel()


async def extract_and_download(
    url: str,
    output_path: Optional[Union[str, Path]] = None,
    progress_cb: ProgressCB = None,
    _skip_validation: bool = False,
) -> MediaResult:
    if _skip_validation:
        validated_url = url
    else:
        validated_url = await asyncio.wait_for(
            asyncio.to_thread(validate_media_url, url),
            timeout=URL_VALIDATION_TIMEOUT,
        )
    logger.info("Bắt đầu download job: %s", redact_url(validated_url))
    parent_dir = Path(output_path) if output_path else DOWNLOAD_DIR
    job_dir = _new_job_dir(parent_dir)
    try:
        result = await _extract_and_download_impl(
            validated_url, job_dir, progress_cb,
        )
        result.cleanup_dir = str(job_dir)
        logger.info("Hoàn tất download job: %s", redact_url(validated_url))
        return result
    except BaseException:
        _remove_path(job_dir)
        raise


async def _extract_and_download_impl(
    url: str,
    output_path: Optional[Union[str, Path]] = None,
    progress_cb: ProgressCB = None,
) -> MediaResult:
    """
    Router tải media đa nền tảng — trả về MediaResult (video hoặc album ảnh).

    - YouTube: yt-dlp → Piped API → VideoDownloadError
    - TikTok (video/photo): tikwm API → yt-dlp → VideoDownloadError
    - Khác: yt-dlp → VideoDownloadError

    progress_cb: callable(pct_or_None, text) — thread-safe, tùy chọn.
    """
    target_dir = Path(output_path) if output_path else DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    # Wrapper: callback chạy từ executor thread → cần schedule về loop của bot
    def _cb(pct, text):
        if not progress_cb:
            return
        try:
            progress_cb(pct, text)
        except Exception:
            pass

    def _as_result(result: Tuple[str, str, int]) -> MediaResult:
        path, title, duration = result
        return MediaResult("video", [path], title=title, duration=duration)

    # ── YouTube routing ──
    if _is_youtube_url(url):
        errors = []

        # Backend 1: yt-dlp (bgutil auto + player clients + cookies)
        try:
            result = await _run_executor(
                loop, _sync_ytdlp_download, (url, target_dir, _cb),
                progress_cb=_cb,
                start_text=(
                    "⏳ Đang lấy thông tin video YouTube...\n"
                    "(Nếu máy chủ bị YouTube chặn, có thể mất 1-2 phút)"
                ),
                heartbeat_text="⏳ Vẫn đang xử lý YouTube, xin chờ...",
            )
            if result:
                return _as_result(result)
        except asyncio.TimeoutError:
            errors.append("yt-dlp: quá thời gian xử lý (IP bị chặn hoặc quá chậm)")
            logger.warning("yt-dlp YouTube timeout, thử Piped API")
        except (VideoTooLargeError, VideoDownloadError) as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            if isinstance(e, VideoTooLargeError):
                raise  # Video quá lớn không phụ thuộc backend
            logger.warning(f"yt-dlp YouTube fail, thử Piped API: {e}")
        except Exception as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            logger.warning(f"yt-dlp YouTube fail, thử Piped API: {e}")

        # Backend 2: Piped API
        try:
            result = await _download_via_piped(url, target_dir, _cb)
            if result:
                logger.info("YouTube tải thành công qua Piped API")
                return _as_result(result)
            errors.append("Piped: không lấy được stream (instance không khả dụng)")
        except VideoTooLargeError:
            raise
        except Exception as e:
            errors.append(f"Piped: {_clean_error(e)}")
            logger.warning(f"Piped API fail: {e}")

        hint = (
            "\n\n💡 <b>IP máy chủ đang bị YouTube chặn.</b> Cách khắc phục:\n"
            "1️⃣ Ưu tiên proxy sạch/residential qua <code>YOUTUBE_PROXY</code>.\n"
            "2️⃣ Hoặc kết nối <code>YOUTUBE_POT_PROVIDER_URL</code> tới provider dùng cùng IP egress.\n"
            "⚠️ Không nên dùng cookie tài khoản Google chính; YouTube có thể khóa tài khoản."
        )
        raise VideoDownloadError(
            "YouTube: tất cả backend đều thất bại.\n"
            + "\n".join(f"• {err}" for err in errors)
            + hint
        )

    # ── TikTok routing ──
    if _is_tiktok_url(url):
        errors = []

        # Backend 1: tikwm API — nhanh, không watermark, ổn định hơn yt-dlp
        # (yt-dlp hay fail/treo trên TikTok với datacenter IP). yt-dlp chỉ là fallback.
        try:
            result = await _download_via_tikwm(url, target_dir, _cb)
            if result:
                return result
            errors.append("tikwm: không lấy được link video (API trả về trống)")
        except VideoTooLargeError:
            raise
        except Exception as e:
            errors.append(f"tikwm: {_clean_error(e)}")
            logger.warning(f"tikwm TikTok fail, thử yt-dlp: {e}")

        # Backend 2: yt-dlp (fast-fail — retries=1, socket_timeout=15)
        try:
            result = await _run_executor(
                loop, _sync_ytdlp_download, (url, target_dir, _cb),
                progress_cb=_cb,
                start_text="⏳ Đang thử tải bằng yt-dlp...",
                heartbeat_text="⏳ Vẫn đang xử lý, xin chờ...",
            )
            if result:
                return _as_result(result)
        except asyncio.TimeoutError:
            errors.append("yt-dlp: quá thời gian xử lý")
            logger.warning("yt-dlp TikTok timeout")
        except (VideoTooLargeError, VideoDownloadError) as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            if isinstance(e, VideoTooLargeError):
                raise  # Video quá lớn không phụ thuộc backend
            logger.warning(f"yt-dlp TikTok fail: {e}")
        except Exception as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            logger.warning(f"yt-dlp TikTok fail: {e}")

        raise VideoDownloadError(
            "TikTok: tất cả backend đều thất bại.\n"
            + "\n".join(f"• {err}" for err in errors)
        )

    # ── Douyin routing (TikTok Trung Quốc) ──
    # tikwm không hỗ trợ Douyin → dùng yt-dlp (có extractor douyin).
    # IP datacenter có thể cần TIKTOK_COOKIES hoặc signature mới tải được.
    if _is_douyin_url(url):
        try:
            result = await _run_executor(
                loop, _sync_ytdlp_download, (url, target_dir, _cb),
                progress_cb=_cb,
                start_text="⏳ Đang lấy thông tin video Douyin (yt-dlp)...",
                heartbeat_text="⏳ Vẫn đang xử lý Douyin, xin chờ...",
            )
            if result:
                return _as_result(result)
        except asyncio.TimeoutError:
            raise VideoDownloadError("Douyin: quá thời gian xử lý (IP bị chặn hoặc quá chậm).")
        except VideoTooLargeError:
            raise
        except Exception as e:
            raise VideoDownloadError(f"Douyin: {_clean_error(e)}") from e
        raise VideoDownloadError("Douyin: không tải được liên kết này.")

    # ── Facebook / nền tảng khác: yt-dlp mặc định ──
    try:
        result = await _run_executor(
            loop, _sync_ytdlp_download, (url, target_dir, _cb),
            progress_cb=_cb,
            start_text="⏳ Đang lấy thông tin video (yt-dlp)...",
            heartbeat_text="⏳ Vẫn đang xử lý, xin chờ...",
        )
        if result:
            return _as_result(result)
    except asyncio.TimeoutError:
        raise VideoDownloadError("Quá thời gian xử lý liên kết này (bị chặn hoặc quá chậm).")
    raise VideoDownloadError("Không thể tải video từ liên kết này.")


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKWARD COMPAT — tests import _sync_extract_and_download
# ═══════════════════════════════════════════════════════════════════════════════

def _sync_extract_and_download(
    url: str, output_dir: Union[str, Path] = DOWNLOAD_DIR
) -> Tuple[str, str, int]:
    """Alias backward-compat — gọi _sync_ytdlp_download trực tiếp (sync)."""
    return _sync_ytdlp_download(url, output_dir)

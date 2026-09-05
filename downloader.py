"""
Module tải video đa nền tảng — kiến trúc Router.

    URL ──► Router ──┬── yt-dlp (PO-token clients + bgutil + cookies)
                     ├── Piped API (YouTube proxy, fallback)
                     ├── tikwm API (TikTok no-watermark, fallback)
                     └── FFmpeg (gộp video+audio)

YÊU CẦU khi deploy lên Render/cloud:
  1. npm + Node >= 20  → cài trong Dockerfile
  2. pip install bgutil-ytdlp-pot-provider + chạy server node build/main.js
     Plugin tự nhận server PO-token ở 127.0.0.1:4416, bypass "Sign in to confirm you're not a bot"
  3. Tùy chọn: YOUTUBE_COOKIES env var (Netscape cookie string) để dùng cookies thật.
"""

import os
import re
import time
import uuid
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import Tuple, Optional, Union

import httpx
import yt_dlp

from config import MAX_FILE_SIZE, DOWNLOAD_DIR, logger


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

TIKTOK_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+", re.IGNORECASE
)
YOUTUBE_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[^\s]+",
    re.IGNORECASE,
)
YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
)

# Player client YouTube không yêu cầu PO Token (thứ tự ưu tiên theo test 2026-09-04)
YOUTUBE_PLAYER_CLIENTS = ["visionos", "android_vr", "tv"]

# Piped API instances (fallback, kiểm tra theo thứ tự)
PIPED_INSTANCES = [
    "api.piped.projectsegfau.lt",
    "piped-api.lunar.icu",
    "pipedapi.kavin.rocks",
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


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _is_tiktok_url(url: str) -> bool:
    """Kiểm tra liên kết có phải TikTok hay không (hỗ trợ cả liên kết rút gọn)."""
    return bool(TIKTOK_URL_PATTERN.search(url))


def _is_youtube_url(url: str) -> bool:
    """Kiểm tra liên kết có phải YouTube hay không (youtu.be / youtube.com/watch)."""
    return bool(YOUTUBE_URL_PATTERN.search(url))


def _extract_youtube_id(url: str) -> Optional[str]:
    """Trích xuất 11-ký tự video ID từ URL YouTube."""
    m = YOUTUBE_ID_PATTERN.search(url)
    return m.group(1) if m else None


def _safe_filename(title: str, unique_id: str, max_len: int = 50) -> str:
    """Tạo tên file an toàn từ tiêu đề video."""
    safe = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title)[:max_len].strip()
    return safe or "video"


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


# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED: HTTP STREAM DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

async def _http_download_stream(
    url: str,
    dest: Path,
    timeout: int = 120,
    progress_cb: ProgressCB = None,
    label: str = "⬇️ Đang tải",
) -> int:
    """Tải file mp4 từ URL bằng httpx streaming. Trả về kích thước bytes. Ném VideoTooLargeError nếu vượt giới hạn."""
    downloaded = 0
    last_report = 0.0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15),
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE:
                        raise VideoTooLargeError(downloaded, MAX_FILE_SIZE)
                    f.write(chunk)
                    # Report mỗi 2s để throttle từ phía stream luôn
                    if progress_cb:
                        now = time.monotonic()
                        if now - last_report >= 2.0:
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
    """Làm sạch thông điệp lỗi: bỏ ANSI codes, giới hạn độ dài."""
    msg = _ANSI_RE.sub("", str(e)).strip()
    if len(msg) > 280:
        msg = msg[:280] + "..."
    return msg


def _build_ytdlp_opts(
    url: str,
    target_dir: Path,
    unique_id: str,
    progress_cb: ProgressCB = None,
) -> dict:
    """Xây dựng ydl_opts cho yt-dlp, tối ưu theo nền tảng."""
    outtmpl = str(target_dir / f"{_safe_filename('%(title)s', unique_id)}_{unique_id}.%(ext)s")

    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        },
    }

    # YouTube: player clients không cần PO token + format <45MB
    if _is_youtube_url(url):
        opts["extractor_args"] = {"youtube": {"player_client": YOUTUBE_PLAYER_CLIENTS}}
        opts["format"] = (
            "best[ext=mp4][filesize_approx<45M]/"
            "bestvideo[ext=mp4][filesize_approx<45M]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        )

    # Optional: YouTube cookies (Netscape format từ YOUTUBE_COOKIES env var)
    cookies_str = os.getenv("YOUTUBE_COOKIES", "").strip()
    if cookies_str:
        if os.path.isfile(cookies_str):
            opts["cookiefile"] = cookies_str
        else:
            cookie_file = Path(tempfile.gettempdir()) / "yt_cookies.txt"
            cookie_file.write_text(cookies_str, encoding="utf-8")
            opts["cookiefile"] = str(cookie_file)
        logger.info("YouTube cookies loaded từ YOUTUBE_COOKIES env var.")

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
    opts = _build_ytdlp_opts(url, target_dir, unique_id, progress_cb)

    downloaded_filepath: Optional[str] = None

    def postprocessor_hook(info: dict) -> None:
        nonlocal downloaded_filepath
        if info.get("status") == "finished":
            downloaded_filepath = (
                info.get("info_dict", {}).get("_filename") or info.get("filepath")
            )

    opts["postprocessor_hooks"] = [postprocessor_hook]

    logger.info(f"yt-dlp đang xử lý: {url}")

    with yt_dlp.YoutubeDL(opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        if not info_dict:
            raise VideoDownloadError("Không thể lấy thông tin từ URL được cung cấp.")

        # Kiểm tra kích thước ước lượng nếu có sẵn
        estimated_size = info_dict.get("filesize") or info_dict.get("filesize_approx")
        if estimated_size and estimated_size > MAX_FILE_SIZE:
            raise VideoTooLargeError(estimated_size, MAX_FILE_SIZE)

        title = info_dict.get("title", "Video")
        duration = int(info_dict.get("duration") or 0)

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

        actual_size = os.path.getsize(final_file)
        if actual_size > MAX_FILE_SIZE:
            try:
                os.remove(final_file)
            except OSError:
                pass
            raise VideoTooLargeError(actual_size, MAX_FILE_SIZE)

        logger.info(f"yt-dlp tải thành công: {final_file} ({actual_size / (1024*1024):.1f}MB)")
        return final_file, title, duration


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKEND 2: Piped API (YouTube proxy, fallback khi yt-dlp bị chặn)
# ═══════════════════════════════════════════════════════════════════════════════

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
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.get(api_url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"Piped [{instance}] fail: {e}")
            continue

        if data.get("error"):
            logger.warning(f"Piped [{instance}] error: {data.get('error')}")
            continue

        title = data.get("title", "YouTube video") or "YouTube video"
        duration = int(data.get("duration") or 0)

        # Tìm video stream tốt nhất (ưu tiên progressive mp4, nếu không thì biggest videoOnly)
        streams = data.get("videoStreams", [])
        audio_streams = data.get("audioStreams", [])

        # Progressive (video+audio merged) — thường ≤720p, không cần ffmpeg
        combined = [s for s in streams if not s.get("videoOnly") and s.get("format") == "MPEG_4"]
        # Video only (mp4)
        video_only = [s for s in streams if s.get("videoOnly") and s.get("format") == "MPEG_4"]

        # Lấy số bitrate/dimension từ quality string để sắp xếp
        def _parse_height(s):
            q = s.get("quality", "0p")
            m = re.search(r"(\d+)p", q)
            return int(m.group(1)) if m else 0

        # Ưu tiên progressive mp4 nhỏ nhất vừa với 49MB (thường 360p/720p progressive)
        if combined:
            combined.sort(key=_parse_height)
            chosen_stream = combined[0]
            need_merge = False
        elif video_only:
            # Nếu cần merge: tải video + audio, rồi ffmpeg
            video_only.sort(key=_parse_height)
            chosen_stream = video_only[0]
            need_merge = bool(audio_streams)
        else:
            logger.warning(f"Piped [{instance}] không tìm thấy mp4 stream")
            continue

        stream_url = chosen_stream.get("url")
        if not stream_url:
            continue

        logger.info(f"Piped [{instance}] tìm thấy stream: {chosen_stream.get('quality')} merge={need_merge}")

        # Tải video stream
        safe_title = _safe_filename(title, unique_id)
        video_path = target_dir / f"{safe_title}_{unique_id}_video.mp4"

        try:
            await _http_download_stream(
                stream_url, video_path, progress_cb=progress_cb,
                label="⬇️ Tải video (Piped)",
            )
        except VideoTooLargeError:
            raise
        except Exception as e:
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
                audio_url = audio_m4a[0].get("url")
                audio_path = target_dir / f"{safe_title}_{unique_id}_audio.m4a"
                try:
                    await _http_download_stream(
                        audio_url, audio_path, progress_cb=progress_cb,
                        label="⬇️ Tải audio (Piped)",
                    )
                except Exception as e:
                    logger.warning(f"Piped [{instance}] tải audio fail: {e}")
                    final_path = str(video_path)
                    await asyncio.sleep(0)
                    return (final_path, title, duration)

                # FFmpeg merge
                final_path = str(target_dir / f"{safe_title}_{unique_id}.mp4")
                try:
                    result = subprocess.run(
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
            actual_size = os.path.getsize(final_path)
            if actual_size > MAX_FILE_SIZE:
                try:
                    os.remove(final_path)
                except OSError:
                    pass
                raise VideoTooLargeError(actual_size, MAX_FILE_SIZE)
            logger.info(f"Piped tải thành công: {final_path} ({actual_size / (1024*1024):.1f}MB)")
            return final_path, title, duration

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKEND 3: TikTok tikwm API (fallback, đã hoạt động 2026-09-04)
# ═══════════════════════════════════════════════════════════════════════════════

async def _download_via_tikwm(
    url: str,
    output_dir: Path,
    progress_cb: ProgressCB = None,
) -> Optional[Tuple[str, str, int]]:
    """Tải TikTok video không watermark qua tikwm API."""
    match = TIKTOK_URL_PATTERN.search(url)
    if not match:
        return None

    tiktok_url = match.group(0)
    if "vm.tiktok.com" in tiktok_url or "vt.tiktok.com" in tiktok_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(tiktok_url)
                tiktok_url = str(resp.url)
        except Exception as e:
            logger.warning(f"Không thể mở rộng liên kết rút gọn TikTok: {e}")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(TIKTOK_FALLBACK_API, data={"url": tiktok_url, "hd": "1"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"TikTok tikwm API không phản hồi: {e}")
        return None

    if data.get("code") != 0 or not data.get("data"):
        logger.warning(f"TikTok tikwm API lỗi: {data.get('msg')}")
        return None

    info = data["data"]
    video_url = info.get("hdplay") or info.get("play")
    if not video_url:
        return None

    title = (info.get("title") or "Video TikTok").strip() or "Video TikTok"
    duration = int(info.get("duration") or 0)

    estimated_size = info.get("size") or 0
    if isinstance(estimated_size, (int, float)) and estimated_size > MAX_FILE_SIZE:
        raise VideoTooLargeError(int(estimated_size), MAX_FILE_SIZE)

    target_dir = output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    unique_id = uuid.uuid4().hex[:10]
    safe_title = _safe_filename(title, unique_id)
    file_path = target_dir / f"{safe_title}_{unique_id}.mp4"

    try:
        await _http_download_stream(
            video_url, file_path, progress_cb=progress_cb,
            label="⬇️ Tải TikTok (tikwm)",
        )
    except VideoTooLargeError:
        _cleanup_leftovers(target_dir, unique_id)
        raise
    except Exception as e:
        _cleanup_leftovers(target_dir, unique_id)
        logger.warning(f"Tải TikTok tikwm stream fail: {e}")
        return None

    logger.info(f"TikTok tikwm tải thành công: {file_path}")
    return str(file_path), title, duration


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTER: extract_and_download()
# ═══════════════════════════════════════════════════════════════════════════════

async def extract_and_download(
    url: str,
    output_path: Optional[Union[str, Path]] = None,
    progress_cb: ProgressCB = None,
) -> Tuple[str, str, int]:
    """
    Router tải video đa nền tảng.

    - YouTube: yt-dlp → Piped API → VideoDownloadError
    - TikTok: yt-dlp → tikwm API → VideoDownloadError
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

    # ── YouTube routing ──
    if _is_youtube_url(url):
        errors = []

        # Backend 1: yt-dlp (bgutil auto + player clients + cookies)
        try:
            result = await loop.run_in_executor(
                None, _sync_ytdlp_download, url, target_dir, _cb
            )
            if result:
                return result
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
                return result
        except VideoTooLargeError:
            raise
        except Exception as e:
            errors.append(f"Piped: {_clean_error(e)}")
            logger.warning(f"Piped API fail: {e}")

        raise VideoDownloadError(
            "YouTube: tất cả backend đều thất bại.\n"
            + "\n".join(f"• {err}" for err in errors)
        )

    # ── TikTok routing ──
    if _is_tiktok_url(url):
        errors = []
        try:
            result = await loop.run_in_executor(
                None, _sync_ytdlp_download, url, target_dir, _cb
            )
            if result:
                return result
        except (VideoTooLargeError, VideoDownloadError) as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            if isinstance(e, VideoTooLargeError):
                raise  # Video quá lớn không phụ thuộc backend
            logger.warning(f"yt-dlp TikTok fail, thử tikwm API: {e}")
        except Exception as e:
            errors.append(f"yt-dlp: {_clean_error(e)}")
            logger.warning(f"yt-dlp TikTok fail, thử tikwm API: {e}")

        # Fallback: tikwm API
        try:
            result = await _download_via_tikwm(url, target_dir, _cb)
            if result:
                return result
            errors.append("tikwm: không lấy được link video (API trả về trống)")
        except VideoTooLargeError:
            raise
        except Exception as e:
            errors.append(f"tikwm: {_clean_error(e)}")

        raise VideoDownloadError(
            "TikTok: tất cả backend đều thất bại.\n"
            + "\n".join(f"• {err}" for err in errors)
        )

    # ── Facebook / nền tảng khác: yt-dlp mặc định ──
    result = await loop.run_in_executor(
        None, _sync_ytdlp_download, url, target_dir, _cb
    )
    if result:
        return result
    raise VideoDownloadError("Không thể tải video từ liên kết này.")


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKWARD COMPAT — tests import _sync_extract_and_download
# ═══════════════════════════════════════════════════════════════════════════════

def _sync_extract_and_download(
    url: str, output_dir: Union[str, Path] = DOWNLOAD_DIR
) -> Tuple[str, str, int]:
    """Alias backward-compat — gọi _sync_ytdlp_download trực tiếp (sync)."""
    return _sync_ytdlp_download(url, output_dir)

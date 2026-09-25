"""
Module progress — hiển thị tiến trình xử lý real-time trên Telegram.

Telegram giới hạn edit tin nhắn (rate limit), không thể update mỗi chunk.
→ TelegramProgress throttle: gửi edit tối thiểu cách nhau MIN_EDIT_INTERVAL.

Cách dùng:
    reporter = TelegramProgress(context, chat_id, "⏳ Đang tải video...")
    await reporter.update(45.2, "Tải 12.3/45.0MB — 2.1MB/s")
    ...
    await reporter.finish("✅ Xong!")   # hoặc .fail("❌ Lỗi")

Callback on_progress cho downloader:
    def cb(pct, text):        # chạy trong thread executor
        reporter.update_sync(pct, text)  # thread-safe qua loop.call_soon_threadsafe
"""

import asyncio
import html
import re
import threading
import time
from typing import Optional

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import logger

MIN_EDIT_INTERVAL = 2.0
MIN_PCT_DELTA = 1.0
BAR_WIDTH = 12
DEFAULT_TITLE = "✦ MEDIA DOWNLOADER"
_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"


def _plain_text(value: Optional[str]) -> str:
    text = html.unescape(value or "")
    return re.sub(r"<[^>]+>", "", text).strip()


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _render_bar(pct: float, width: int = BAR_WIDTH) -> str:
    value = max(0.0, min(100.0, float(pct)))
    filled = int(value * width / 100)
    return f"{'▰' * filled}{'▱' * (width - filled)} {value:.0f}%"


def _render_progress_message(
    title: str,
    stage: str,
    detail: str = "",
    pct: Optional[float] = None,
    source: str = "",
    elapsed: float = 0.0,
) -> str:
    clean_title = _plain_text(title) or DEFAULT_TITLE
    clean_stage = _plain_text(stage)
    clean_detail = _plain_text(detail)
    clean_source = _plain_text(source)
    lines = [
        f"<b>{html.escape(clean_title.upper())}</b>",
        _SEPARATOR,
    ]
    if clean_stage:
        lines.append(f"<i>{html.escape(clean_stage)}</i>")
    if pct is not None:
        lines.append(f"<code>{_render_bar(pct)}</code>")
        meta = []
        if elapsed:
            meta.append(f"⏱ {html.escape(_format_duration(elapsed))}")
        if pct is not None and 0 < pct < 100 and elapsed > 0:
            eta = elapsed * (100 - pct) / pct
            meta.append(f"ETA {html.escape(_format_duration(eta))}")
        if clean_source:
            meta.append(f"🔗 {html.escape(clean_source)}")
        if meta:
            lines.append(" · ".join(meta))
    elif clean_source:
        lines.append(f"🔗 <code>{html.escape(clean_source)}</code>")
    if clean_detail:
        lines.extend(("", html.escape(clean_detail[:260])))
    return "\n".join(lines)


def _stage_from_detail(detail: str) -> str:
    text = _plain_text(detail)
    if text.startswith("🔧"):
        return "Đang xử lý video"
    if text.startswith("⬇️"):
        return "Đang tải video"
    if text.startswith("⏳"):
        return "Đang lấy thông tin video"
    return ""


class TelegramProgress:
    """Edit một tin nhắn trạng thái theo % tiến trình, throttle theo thời gian + %."""

    def __init__(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        initial_text: str = "Đang xử lý",
        title: str = "",
        source: str = "",
    ) -> None:
        self.context = context
        self.chat_id = chat_id
        self.title = _plain_text(title) or DEFAULT_TITLE
        self.source = _plain_text(source)
        self.stage = _plain_text(initial_text) or "Đang xử lý"
        self.detail = ""
        self._pct = 0.0
        self._message = None
        self._last_edit = 0.0
        self._last_pct = -100.0
        self._finished = False
        self._started_at = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._update_lock = threading.Lock()
        self._update_scheduled = False
        self._create_task = asyncio.create_task(self._send())

    def _message_text(self) -> str:
        return _render_progress_message(
            self.title,
            self.stage,
            self.detail,
            self._pct,
            self.source,
            time.monotonic() - self._started_at,
        )

    async def _send(self) -> bool:
        try:
            self._message = await self.context.bot.send_message(
                chat_id=self.chat_id,
                text=self._message_text(),
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            self._message = None
            logger.warning("Không tạo được progress message: %s", exc)
            return False

    async def _edit(
        self,
        force: bool = False,
        pct: Optional[float] = None,
    ) -> bool:
        if self._finished or self._message is None:
            return False
        now = time.monotonic()
        if not force:
            if now - self._last_edit < MIN_EDIT_INTERVAL:
                return False
            if pct is not None:
                if pct < self._last_pct:
                    self._last_pct = -100.0
                if pct - self._last_pct < MIN_PCT_DELTA:
                    return False
        self._last_edit = now
        self._last_pct = self._pct
        try:
            await self._message.edit_text(self._message_text(), parse_mode=ParseMode.HTML)
            return True
        except Exception as exc:
            logger.warning("Không cập nhật được progress message: %s", exc)
            return False

    async def configure(self, title: str, source: str = "") -> bool:
        self.title = _plain_text(title) or DEFAULT_TITLE
        self.source = _plain_text(source) or self.source
        return await self._edit(force=True)

    async def set_stage(
        self,
        stage: str,
        detail: str = "",
        pct: Optional[float] = None,
    ) -> bool:
        self.stage = _plain_text(stage) or self.stage
        self.detail = _plain_text(detail)
        if pct is not None:
            self._pct = max(0.0, min(100.0, float(pct)))
        return await self._edit(force=True)

    async def update(self, pct: Optional[float], text: Optional[str] = None) -> bool:
        if self._finished:
            return False
        if pct is not None:
            self._pct = max(0.0, min(100.0, float(pct)))
        if text is not None:
            clean_text = _plain_text(text)
            inferred_stage = _stage_from_detail(clean_text)
            if inferred_stage:
                self.stage = inferred_stage
            self.detail = clean_text
        return await self._edit(pct=pct)

    async def complete(self, headline: str, detail: str = "") -> bool:
        if self._message is None:
            return False
        self._finished = True
        clean_headline = _plain_text(headline) or "Đã xử lý xong"
        clean_detail = _plain_text(detail)
        lines = [
            "✅ <b>HOÀN TẤT</b>",
            _SEPARATOR,
            f"<b>{html.escape(clean_headline)}</b>",
        ]
        if clean_detail:
            lines.extend(("", html.escape(clean_detail[:260])))
        try:
            await self._message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return True
        except Exception as exc:
            logger.warning("Không hoàn tất được progress message: %s", exc)
            return False

    async def finish(self, text: str) -> bool:
        if self._finished or self._message is None:
            return False
        self._finished = True
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.HTML)
            return True
        except Exception as exc:
            logger.warning("Không hoàn tất được progress message: %s", exc)
            return False

    async def fail(self, text: str) -> bool:
        if self._message is None:
            return False
        self._finished = True
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.HTML)
            return True
        except Exception as exc:
            logger.warning("Không báo lỗi qua progress message: %s", exc)
            return False

    async def delete(self) -> bool:
        self._finished = True
        if self._message is None:
            return False
        try:
            await self._message.delete()
            return True
        except Exception as exc:
            logger.warning("Không xóa được progress message: %s", exc)
            return False

    async def _update_from_sync(
        self, pct: Optional[float], text: Optional[str]
    ) -> None:
        try:
            await self.update(pct, text)
        finally:
            with self._update_lock:
                self._update_scheduled = False

    def update_sync(self, pct: Optional[float], text: Optional[str] = None) -> None:
        with self._update_lock:
            if self._update_scheduled:
                return
            self._update_scheduled = True
        try:
            asyncio.run_coroutine_threadsafe(
                self._update_from_sync(pct, text), self._loop
            )
        except Exception:
            with self._update_lock:
                self._update_scheduled = False

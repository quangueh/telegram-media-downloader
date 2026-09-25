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
import threading
import time
from typing import Optional

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import logger

# Tối thiểu giữa 2 lần edit — tránh Telegram "Too Many Requests"
MIN_EDIT_INTERVAL = 3.0
# Bỏ qua update nếu % tăng < ngưỡng này (kể cả đã đến interval)
MIN_PCT_DELTA = 1.0


class TelegramProgress:
    """Edit một tin nhắn trạng thái theo % tiến trình, throttle theo thời gian + %."""

    def __init__(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        initial_text: str = "⏳ Đang xử lý...",
        title: str = "",
    ) -> None:
        self.context = context
        self.chat_id = chat_id
        self.title = title
        self._message = None
        self._last_edit = 0.0
        self._last_pct = -100.0
        self._finished = False
        self._base_text = initial_text
        # Capture event loop TẠI THỜI ĐIỂM TẠO (chạy trong async context)
        # → update_sync từ executor thread dùng loop này
        self._loop = asyncio.get_running_loop()
        self._update_lock = threading.Lock()
        self._update_scheduled = False
        self._create_task = asyncio.create_task(self._send(initial_text))

    async def _send(self, text: str) -> bool:
        try:
            self._message = await self.context.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            self._message = None
            logger.warning("Không tạo được progress message: %s", exc)
            return False

    async def update(self, pct: Optional[float], text: Optional[str] = None) -> bool:
        if self._finished or self._message is None:
            return False
        now = time.monotonic()
        if now - self._last_edit < MIN_EDIT_INTERVAL:
            return False
        if pct is not None:
            if pct < self._last_pct:
                self._last_pct = -100.0
            if pct - self._last_pct < MIN_PCT_DELTA:
                return False
            self._last_pct = pct
        self._last_edit = now

        bar = _render_bar(pct) if pct is not None else ""
        content = text or ""
        msg = f"{html.escape(self.title)}\n{bar}\n{html.escape(content)}".strip()
        try:
            await self._message.edit_text(msg, parse_mode=ParseMode.HTML)
            return True
        except Exception as exc:
            logger.warning("Không cập nhật được progress message: %s", exc)
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


def _render_bar(pct: float, width: int = 10) -> str:
    """Render progress bar 10 ký tự: ▰▰▰▱▱▱▱▱▱▱ 45%"""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(pct * width / 100)
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {pct:.0f}%"

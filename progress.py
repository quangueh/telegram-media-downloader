"""
Module progress — hiển thị tiến trình xử lý real-time trên Telegram.

Telegram giới hạn edit tin nhắn (rate limit), không thể update mỗi chunk.
→ TelegramProgress throttle: gửi edit tối thiểu cách nhau MIN_EDIT_INTERVAL.

Cách dùng:
    reporter = TelegramProgress(bot, chat_id, "⏳ Đang tải video...")
    await reporter.update(45.2, "Tải 12.3/45.0MB — 2.1MB/s")
    ...
    await reporter.finish("✅ Xong!")   # hoặc .fail("❌ Lỗi")

Callback on_progress cho downloader:
    def cb(pct, text):        # chạy trong thread executor
        reporter.update_sync(pct, text)  # thread-safe qua loop.call_soon_threadsafe
"""

import asyncio
import time
from typing import Optional

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Tối thiểu giữa 2 lần edit — tránh Telegram "Too Many Requests"
MIN_EDIT_INTERVAL = 2.0
# Bỏ qua update nếu % tăng < ngưỡng này (kể cả đã đến interval)
MIN_PCT_DELTA = 2.0


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
        self._create_task = asyncio.create_task(self._send(initial_text))

    async def _send(self, text: str) -> None:
        try:
            self._message = await self.context.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            self._message = None

    async def update(self, pct: Optional[float], text: Optional[str] = None) -> None:
        """Cập nhật % + text. Throttle: ≥2s kể từ lần edit cuối và ≥2% tăng thêm."""
        if self._finished or self._message is None:
            return
        now = time.monotonic()
        # Điều kiện throttle: đủ interval HOẶC pct tăng đáng kể nhưng vẫn ≥1s
        if now - self._last_edit < MIN_EDIT_INTERVAL:
            return
        if pct is not None:
            if pct - self._last_pct < MIN_PCT_DELTA:
                return
            self._last_pct = pct
        self._last_edit = now

        bar = _render_bar(pct) if pct is not None else ""
        content = text or ""
        msg = f"{self.title}\n{bar}\n{content}".strip()
        try:
            await self._message.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    async def finish(self, text: str) -> None:
        """Edit lần cuối — xóa thanh progress, chỉ để kết quả."""
        if self._finished or self._message is None:
            return
        self._finished = True
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    async def fail(self, text: str) -> None:
        """Giống finish nhưng giữ chế độ HTML parse (lỗi thường chứa <b>)."""
        if self._finished or self._message is None:
            return
        self._finished = True
        try:
            await self._message.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def delete(self) -> None:
        """Xóa tin nhắn progress (dùng sau khi gửi xong media)."""
        self._finished = True
        if self._message is None:
            return
        try:
            await self._message.delete()
        except Exception:
            pass

    def update_sync(self, pct: Optional[float], text: Optional[str] = None) -> None:
        """Callback chạy từ thread executor — schedule update về event loop chính."""
        try:
            asyncio.run_coroutine_threadsafe(
                self.update(pct, text), self._loop
            )
        except Exception:
            pass  # loop đã đóng (bot đang shutdown) — bỏ qua


def _render_bar(pct: float, width: int = 10) -> str:
    """Render progress bar 10 ký tự: ▰▰▰▱▱▱▱▱▱▱ 45%"""
    filled = int(pct * width / 100)
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {pct:.0f}%"

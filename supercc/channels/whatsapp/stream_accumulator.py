"""WhatsApp 流式缓冲 — 累积文本块并批量发送。"""
from __future__ import annotations

import asyncio
from typing import Callable, Awaitable


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to WhatsApp in batches.

    WhatsApp message updates are expensive (one API call per message), so we buffer
    chunks and flush when a tool call arrives or after a short idle period.
    """

    def __init__(self, chat_id: str, message_id: str, send_fn: Callable[[str], Awaitable[None]], flush_timeout: float = 1.5):
        self.chat_id = chat_id
        self._message_id = message_id
        self._send = send_fn
        self._flush_timeout = flush_timeout
        self._buffer = ""
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None
        self.sent_something = False

    async def add_text(self, text: str) -> None:
        """Append text chunk and (re)start the flush timer."""
        if not text:
            return
        async with self._lock:
            self._buffer += text
            if self._timer_task:
                self._timer_task.cancel()
            self._timer_task = asyncio.create_task(self._flush_after(self._flush_timeout))

    async def flush(self) -> None:
        """Send accumulated text immediately."""
        async with self._lock:
            if self._timer_task:
                self._timer_task.cancel()
                self._timer_task = None
            if self._buffer:
                text = self._buffer
                self._buffer = ""
                if text.strip():
                    await self._send(text)
                    self.sent_something = True

    async def _flush_after(self, delay: float) -> None:
        """Flush after a delay, but cancel if more text arrives."""
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._buffer:
                    text = self._buffer
                    self._buffer = ""
                    if text.strip():
                        await self._send(text)
                        self.sent_something = True
        except asyncio.CancelledError:
            pass
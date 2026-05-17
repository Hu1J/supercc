"""WeChat error notifier — sends WARNING/ERROR logs to the active WeChat chat.

A module-level WeChat logging handler captures all warnings and errors (including
those from third-party SDKs like httpx, etc.) and forwards them as
WeChat messages to the most recently active chat so the user is always aware
of what is happening.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

# Global state — set once at startup
_wechat_client: Optional["WeChatClient"] = None
_last_chat_id: Optional[str] = None
_lock = threading.Lock()


def setup(wechat_client: "WeChatClient") -> None:
    """Initialize the notifier with a WeChatClient. Call once at startup."""
    global _wechat_client
    _wechat_client = wechat_client
    _install_handler()


def update_chat_id(chat_id: str) -> None:
    """Update the target chat_id for error notifications."""
    global _last_chat_id
    with _lock:
        _last_chat_id = chat_id


def get_chat_id() -> Optional[str]:
    with _lock:
        return _last_chat_id


def _install_handler() -> None:
    """Register the WeChatHandler on the root logger."""
    handler = _WeChatHandler()
    handler.setLevel(logging.WARNING)
    # Format: timestamp + level + message (no logger name clutter)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    # Don't propogate to avoid double-printing
    handler.setLevel(logging.WARNING)


class _WeChatHandler(logging.Handler):
    """Logging handler that sends WARNING/ERROR records to the active WeChat chat."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            chat_id = get_chat_id()
            if not chat_id or not _wechat_client:
                return

            # Skip the bridge's own INFO logs (already visible in WeChat)
            if record.levelno < logging.WARNING:
                return

            msg = self.format(record)
            # Truncate very long messages
            if len(msg) > 1500:
                msg = msg[:1500] + "…"

            level_tag = {
                logging.WARNING: "⚠️",
                logging.ERROR: "❌",
                logging.CRITICAL: "🚨",
            }.get(record.levelno, "🔔")

            text = f"{level_tag} {msg}"

            # Fire-and-forget — don't block the logging thread.
            # run_coroutine_threadsafe properly schedules async functions from any thread.
            try:
                loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(_send_async(text), loop)
            except Exception:
                # Never let logging errors propagate
                pass
        except Exception:
            # Never let logging errors propagate
            pass


async def _send_async(text: str) -> None:
    """Send error text to WeChat asynchronously."""
    if not _wechat_client or not _last_chat_id:
        return
    try:
        await _wechat_client.send_post_reply(
            chat_id=_last_chat_id,
            content=text,
            log_reply=False,
        )
    except Exception:
        pass

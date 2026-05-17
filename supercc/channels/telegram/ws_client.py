"""Telegram WebSocket/LongPolling client using python-telegram-bot."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CallbackContext

logger = logging.getLogger("telegram")

MessageCallback = Callable[[dict], Awaitable[None]]


class TelegramWSClient:
    """Manages connection to Telegram via python-telegram-bot Application."""

    def __init__(self, bot_token: str, on_message: MessageCallback | None = None):
        self._bot_token = bot_token
        self._on_message = on_message
        self._app: Application | None = None
        self._running = False

    async def _handle_update(self, update: Update, context: CallbackContext):
        """Handle incoming Telegram message."""
        if not update.message or not update.message.text:
            return

        # Build message body dict (similar to WeCom/Feishu format)
        body = {
            "msgid": str(update.message.message_id),
            "chat_id": str(update.message.chat_id),
            "user_id": str(update.message.from_user.id),
            "username": update.message.from_user.username or "",
            "content": update.message.text,
            "date": update.message.date.isoformat() if update.message.date else "",
            "chat_type": "group" if update.message.chat.type in ("group", "supergroup") else "private",
        }

        if self._on_message:
            await self._on_message(body)

    def start(self) -> None:
        """Start the Telegram bot (non-blocking)."""
        if self._running:
            return

        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_update)
        )
        self._running = True

        # Run polling in background thread
        import threading
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._app.run_polling(drop_pending_updates=True))
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        logger.info("Telegram WS client started (polling)")

    async def close(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        if self._app:
            await self._app.stop()
            await self._app.updater.stop()
"""Telegram client for sending messages."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger("telegram")

# Characters that need escaping in Telegram MarkdownV2
_MD2_ESCAPE_CHARS = r'_*[]()~`>#+\-=|{}.!'


def _escape_markdown_v2(text: str) -> str:
    """Escape characters for Telegram MarkdownV2 parse mode.

    Per Telegram docs, the following characters need to be escaped:
    _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    # We only escape if the character is not already escaped
    result = []
    for ch in text:
        if ch in _MD2_ESCAPE_CHARS:
            result.append(f"\\{ch}")
        else:
            result.append(ch)
    return "".join(result)


@dataclass
class IncomingMessage:
    """Parsed incoming message from Telegram."""
    message_id: str
    chat_id: str
    user_id: str
    username: str
    content: str
    chat_type: str = "private"  # "private" | "group"
    raw_content: str = ""


class TelegramClient:
    """Client for sending messages to Telegram."""

    def __init__(self, bot_token: str):
        self._bot = Bot(bot_token)

    async def send_text(
        self, chat_id: int | str, text: str, reply_to: int | str | None = None
    ) -> str:
        """Send a text message. Returns message_id."""
        msg = await self._bot.send_message(
            chat_id=int(chat_id),
            text=_escape_markdown_v2(text),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=int(reply_to) if reply_to else None,
        )
        return str(msg.message_id)

    async def send_markdown(
        self, chat_id: int | str, text: str, reply_to: int | str | None = None
    ) -> str:
        """Send markdown text message (MarkdownV2). Returns message_id."""
        return await self.send_text(chat_id, text, reply_to)

    async def add_typing_reaction(self, chat_id: int | str) -> None:
        """Send typing indicator."""
        await self._bot.send_chat_action(chat_id=int(chat_id), action="typing")
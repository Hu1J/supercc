"""WeCom AIBot client for sending and receiving messages."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from supercc.adapter.wecom.ws_client import WeComWSClient

logger = logging.getLogger(__name__)


@dataclass
class WeComIncomingMessage:
    """Parsed incoming message from WeCom."""
    message_id: str
    chat_id: str
    user_open_id: str
    content: str
    message_type: str
    create_time: str = ""
    parent_id: str = ""
    thread_id: str = ""
    raw_content: str = ""
    is_group_chat: bool = False
    chat_type: str = "single"
    mention_bot: bool = False
    mention_ids: list[str] = None
    group_name: str = ""

    def __post_init__(self):
        if self.mention_ids is None:
            self.mention_ids = []


class WeComClient:
    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        bot_name: str = "Claude",
        data_dir: str = "",
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self.bot_name = bot_name
        self.data_dir = data_dir
        self._ws_client: WeComWSClient | None = None
        self._user_name_cache: dict[str, str] = {}

    def set_ws_client(self, ws_client: WeComWSClient) -> None:
        """Bind the WebSocket client for message sending."""
        self._ws_client = ws_client

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send a text message."""
        await self._send(chat_id, {"msgtype": "text", "text": {"content": text}})
        return ""

    async def send_markdown(self, chat_id: str, text: str) -> str:
        """Send a markdown message."""
        await self._send(chat_id, {"msgtype": "markdown", "markdown": {"content": text}})
        return ""

    async def send_image(self, chat_id: str, image_url: str) -> str:
        """Send an image message (via URL or base64)."""
        # WeCom supports image by URL or base64; simplified here
        await self._send(chat_id, {"msgtype": "image", "image": {"url": image_url}})
        return ""

    async def send_file(self, chat_id: str, file_url: str, file_name: str = "") -> str:
        """Send a file message."""
        await self._send(chat_id, {"msgtype": "file", "file": {"url": file_url, "name": file_name}})
        return ""

    async def send_text_reply(self, chat_id: str, text: str, reply_to_message_id: str = "") -> str:
        """Send a text reply."""
        return await self.send_text(chat_id, text)

    async def send_markdown_reply(self, chat_id: str, text: str, reply_to_message_id: str = "") -> str:
        """Send a markdown reply."""
        return await self.send_markdown(chat_id, text)

    async def send_image_reply(self, chat_id: str, image_url: str, reply_to_message_id: str = "") -> str:
        """Send an image reply."""
        return await self.send_image(chat_id, image_url)

    async def send_file_reply(self, chat_id: str, file_url: str, file_name: str, reply_to_message_id: str = "") -> str:
        """Send a file reply."""
        return await self.send_file(chat_id, file_url, file_name)

    async def send_interactive(self, chat_id: str, card: dict, reply_to_message_id: str = "") -> str:
        """Send a template card message (replying to a message)."""
        await self._send(chat_id, {"msgtype": "template_card", "template_card": card})
        return ""

    async def send_interactive_reply(self, chat_id: str, markdown_text: str, reply_to_message_id: str = "", log_reply: bool = True) -> str:
        """Send a markdown message as a reply."""
        return await self.send_markdown_reply(chat_id, markdown_text, reply_to_message_id)

    async def send_card(self, chat_id: str, card: dict) -> str:
        """Send an interactive card as a standalone message."""
        await self._send(chat_id, {"msgtype": "template_card", "template_card": card})
        return ""

    async def send_edit_diff_card(self, chat_id: str, card: dict, reply_to_message_id: str = "", log_reply: bool = True) -> str:
        """Send a pre-built diff card."""
        return await self.send_interactive(chat_id, card, reply_to_message_id)

    async def _send(self, chat_id: str, payload: dict) -> None:
        if not self._ws_client:
            logger.warning("WS client not bound, cannot send message")
            return
        try:
            await self._ws_client.send_message(chat_id, payload)
        except Exception as e:
            logger.warning(f"Failed to send WeCom message: {e}")

    async def get_message(self, message_id: str) -> dict | None:
        """Fetch a message by ID. WeCom API may not support this; return None."""
        return None

    async def download_media(self, message_id: str, file_key: str, msg_type: str = "image") -> bytes:
        """Download media. WeCom API may require specific implementation."""
        return b""

    async def get_chat_history(self, chat_id: str, limit: int = 20) -> list:
        """Fetch recent messages. WeCom may not support this."""
        return []

    async def get_chat_members(self, chat_id: str) -> list:
        """Fetch chat members. WeCom may not support this."""
        return []

    async def get_user_name(self, user_id: str) -> str:
        """Fetch user display name. Fallback to cached value or user_id."""
        if not user_id:
            return ""
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]
        self._user_name_cache[user_id] = user_id
        return user_id

    async def add_typing_reaction(self, message_id: str, emoji_type: str = "OK") -> str | None:
        """No-op: WeCom does not have typing indicator."""
        return None

    async def remove_typing_reaction(self, message_id: str, reaction_id: str) -> None:
        """No-op: WeCom does not have typing indicator."""
        pass

    async def check_group_permissions(self, chat_id: str) -> dict:
        """No-op: WeCom does not require this check."""
        return {"history_ok": True, "members_ok": True, "auth_url": ""}

    def parse_incoming_message(self, body: dict) -> WeComIncomingMessage | None:
        """Parse webhook payload into WeComIncomingMessage."""
        try:
            msgid = body.get("msgid") or body.get("msgId") or ""
            chattype = body.get("chattype") or body.get("chatType") or "single"
            chatid = body.get("chatid") or body.get("chatId") or ""
            from_data = body.get("from") or {}
            userid = from_data.get("userid") or from_data.get("userId") or "" if isinstance(from_data, dict) else ""
            msgtype = body.get("msgtype") or body.get("msgType") or "text"

            content = ""
            if msgtype == "text":
                text_data = body.get("text") or {}
                content = text_data.get("content") or "" if isinstance(text_data, dict) else ""
            elif msgtype == "markdown":
                md_data = body.get("markdown") or {}
                content = md_data.get("content") or "" if isinstance(md_data, dict) else ""

            return WeComIncomingMessage(
                message_id=msgid,
                chat_id=chatid,
                user_open_id=userid,
                content=content,
                message_type=msgtype,
                create_time=str(body.get("createTime") or body.get("create_time") or ""),
                raw_content=json.dumps(body, ensure_ascii=False),
                is_group_chat=chattype == "group",
                chat_type=chattype,
            )
        except Exception as e:
            logger.error(f"Failed to parse incoming message: {e}")
            return None

    def _extract_content(self, message) -> str:
        """Extract text content from a message."""
        if isinstance(message, dict):
            msg_type = message.get("msg_type", "")
            content_str = message.get("content", "{}")
        else:
            msg_type = getattr(message, "msg_type", "") or ""
            content_str = getattr(message, "content", "{}")
        try:
            content = json.loads(content_str)
            if msg_type == "text":
                return content.get("text", "")
            elif msg_type == "markdown":
                return content.get("content", "")
            return str(content)
        except Exception:
            return content_str

"""WhatsApp 消息发送客户端。"""
from __future__ import annotations

import logging
from supercc.channels.whatsapp.bridge_client import WhatsAppBridgeClient

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """
    WhatsApp 消息发送客户端。

    封装 WhatsAppBridgeClient，提供：
    - send_text：发送文本消息
    - send_image：发送图片
    """

    def __init__(self, bridge_port: int = 3000):
        self._bridge = WhatsAppBridgeClient(port=bridge_port)

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str:
        """发送文本消息。"""
        return await self._bridge.send_text(chat_id, text, reply_to)

    async def send_image(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> str:
        """发送图片。"""
        return await self._bridge.send_image(chat_id, file_path, caption)

    async def set_typing(self, chat_id: str) -> None:
        """发送 typing 提示。"""
        await self._bridge.set_typing(chat_id)

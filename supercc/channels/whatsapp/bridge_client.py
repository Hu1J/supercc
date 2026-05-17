"""WhatsApp Bridge HTTP Client — 与 Node.js bridge 通信。"""
from __future__ import annotations

import aiohttp
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default port matches hermes-agent bridge default
DEFAULT_BRIDGE_PORT = 3000


class WhatsAppBridgeClient:
    """
    WhatsApp Bridge HTTP 客户端。

    通过 HTTP 与 Node.js bridge 进程通信：
    - GET  /messages — 长轮询收消息
    - POST /send     — 发文本
    - POST /send-media — 发媒体
    - GET  /health   — 健康检查
    """

    def __init__(self, port: int = DEFAULT_BRIDGE_PORT):
        self._port = port
        self._base = f"http://127.0.0.1:{port}"

    async def get_messages(self) -> list[dict[str, Any]]:
        """长轮询获取新消息。"""
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{self._base}/messages") as resp:
                if resp.status != 200:
                    logger.warning("Bridge /messages returned %s", resp.status)
                    return []
                return await resp.json()

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str:
        """
        发送文本消息。

        Returns:
            message_id on success, empty string on failure.
        """
        payload: dict[str, Any] = {"chatId": chat_id, "message": text}
        if reply_to:
            payload["replyTo"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(f"{self._base}/send", json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.warning("Bridge /send failed %s: %s", resp.status, err)
                    return ""
                data = await resp.json()
                return data.get("messageId", "")

    async def send_image(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> str:
        """发送图片。"""
        data = aiohttp.FormData()
        data.add_field("chatId", chat_id)
        if caption:
            data.add_field("caption", caption)
        data.add_field("file", open(file_path, "rb"), filename=file_path.split("/")[-1])

        async with aiohttp.ClientSession() as sess:
            async with sess.post(f"{self._base}/send-media", data=data) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.warning("Bridge /send-media failed %s: %s", resp.status, err)
                    return ""
                data = await resp.json()
                return data.get("messageId", "")

    async def set_typing(self, chat_id: str) -> None:
        """发送 typing 提示。"""
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{self._base}/typing", json={"chatId": chat_id}
            ) as resp:
                pass  # Best effort

    async def health_check(self) -> dict[str, Any]:
        """健康检查。"""
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{self._base}/health") as resp:
                if resp.status != 200:
                    return {"status": "error", "code": resp.status}
                return await resp.json()

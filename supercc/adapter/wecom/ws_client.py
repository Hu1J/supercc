"""企业微信 WebSocket 客户端（基于官方 wecom-aibot-sdk-python）。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from wecom_aibot_sdk import WSClient, WSClientOptions, DefaultLogger, WsFrame

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 客户端。

    基于官方 wecom-aibot-sdk-python，封装了连接管理、消息接收、事件分发。
    """

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        on_message: MessageCallback | None = None,
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._on_message = on_message
        self._client = WSClient(
            WSClientOptions(
                bot_id=bot_id,
                secret=bot_secret,
                logger=DefaultLogger(),
            )
        )
        # 存储 frame，供 core_client 通过 message_id 查找原始 frame（用于 reply_stream）
        self._frame_by_msg_id: dict[str, WsFrame] = {}
        self._lock = asyncio.Lock()
        self._register_handlers()

    def _register_handlers(self):
        """注册 SDK 事件处理器。"""
        for msg_type in ("text", "image", "file", "voice", "mixed"):
            self._client.on(f"message.{msg_type}", self._handle_message)
        # 事件回调（template_card_event 等）
        self._client.on("event", self._handle_event)

    async def _handle_message(self, frame: WsFrame):
        """处理普通消息事件。"""
        body = frame.body
        if not body:
            return
        msg_id = body.get("msgid", "")
        if msg_id:
            async with self._lock:
                self._frame_by_msg_id[msg_id] = frame
        if self._on_message:
            await self._on_message(body)

    async def _handle_event(self, frame: WsFrame):
        """处理事件回调。"""
        body = frame.body
        if not body:
            return
        msg_id = body.get("msgid", "")
        if msg_id:
            async with self._lock:
                self._frame_by_msg_id[msg_id] = frame
        if self._on_message:
            await self._on_message(body)

    def get_frame(self, msg_id: str) -> WsFrame | None:
        """根据 message_id 查找原始 frame（用于 reply_stream）。"""
        return self._frame_by_msg_id.get(msg_id)

    def pop_frame(self, msg_id: str) -> WsFrame | None:
        """弹出并删除 frame（用完即弃）。"""
        frame = self._frame_by_msg_id.pop(msg_id, None)
        return frame

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    def start(self):
        """启动连接（非阻塞）。"""
        self._client.connect()

    async def close(self):
        """断开连接。"""
        await self._client.disconnect()

"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.protocol import JsonRpcRequest, Event
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable

logger = logging.getLogger(__name__)


class WeComCoreWSClient:
    """
    企业微信插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 WeCom 消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为企业微信格式并发送
    """

    def __init__(
        self,
        core_url: str,
        wecom_client: WeComClient,
        bot_id: str,
        project_path: str,
    ):
        self.core_url = core_url
        self.wecom = wecom_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._id_counter = 0

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[WeComCore] Connected to core")
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """持续读取核心发来的消息。"""
        import websockets
        while self._running and self._ws:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)
                await self._handle_core_message(data)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception:
                logger.exception("[WeComCore] Error reading message")

    async def _handle_core_message(self, data: dict):
        """处理核心发来的消息（Response 或 Event）。"""
        if "id" in data:
            req_id = str(data.get("id"))
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为企业微信格式并发送。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        card = params.get("extra", {}).get("card")

        if not content:
            return

        # WeCom 原生支持 Markdown；Command card 降级为 markdown 发送（无卡片的平台）
        await self.wecom.send_markdown(chat_id, content)

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心，并等待响应。"""
        inbound = incoming_to_inbound(msg, bot_id=self.bot_id, project_path=self.project_path)

        req = JsonRpcRequest(
            id=self._next_id(),
            method="wecom.message",
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": inbound.user_open_id,
                "platform": inbound.session_key.platform,
                "project_path": inbound.session_key.project_path,
                "content": inbound.content,
                "message_type": inbound.message_type.value,
                "is_group_chat": inbound.extra.get("is_group_chat", False),
                "mention_bot": inbound.extra.get("mention_bot", False),
                "mention_ids": inbound.extra.get("mention_ids", []),
                "group_name": inbound.extra.get("group_name", ""),
                "extra": inbound.extra,
            },
        )

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        await self._ws.send(json.dumps(req.to_dict()))
        result = await future
        return result or {}

    async def _send_event(self, method: str, params: dict):
        """发送 Event notification 到核心。"""
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._ws:
            await self._ws.send(json.dumps(frame))

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()

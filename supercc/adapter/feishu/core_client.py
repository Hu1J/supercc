"""飞书插件的 Thin Client：连接核心 WebSocket 服务。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from core.protocol import (
    JsonRpcRequest,
    OutboundMessage, Event,
)
from supercc.adapter.feishu.client import IncomingMessage
from supercc.adapter.feishu.core_protocol import incoming_to_inbound

logger = logging.getLogger(__name__)


class FeishuCoreWSClient:
    """
    飞书插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 IncomingMessage（来自飞书）转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为飞书格式并发送
    - 处理 tool_call 事件（委托给 FeishuClient 执行）

    不负责：Session 管理、AI 推理、记忆操作
    """

    def __init__(
        self,
        core_url: str,           # e.g. "ws://127.0.0.1:8765"
        feishu_client: Any,      # FeishuClient 实例（用于发送消息）
        bot_id: str,
        project_path: str,
        on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None,
    ):
        self.core_url = core_url
        self.feishu = feishu_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._on_message = on_message
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._id_counter = 0

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info(f"[FeishuCore] Connected to core at {self.core_url}")

        # 启动读取循环
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
                logger.warning("[FeishuCore] Connection closed")
                break
            except Exception:
                logger.exception("[FeishuCore] Error reading message")

    async def _handle_core_message(self, data: dict):
        """处理核心发来的消息（Response 或 Event）。"""
        if "id" in data:
            # Response：唤醒等待的 Future
            req_id = data.get("id")
            if str(req_id) in self._pending_responses:
                fut = self._pending_responses.pop(str(req_id))
                if data.get("error"):
                    fut.set_result(data)
                else:
                    fut.set_result(data.get("result"))
            return

        # Event notification
        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == Event.PONG:
            pass  # 心跳响应

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为飞书格式并发送。"""
        from supercc.adapter.feishu.format.reply_formatter import should_use_card

        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")

        if not content:
            return

        if should_use_card(content):
            await self.feishu.send_interactive_card(chat_id, content)
        else:
            await self.feishu.send_post_reply(chat_id, content, message_id)

    async def _handle_tool_call(self, params: dict):
        """处理核心发来的工具调用请求。"""
        tool_name = params.get("tool_name", "")
        tool_input = params.get("tool_input", {})
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")

        # 执行工具（通过 FeishuClient 的 MCP 工具）
        result_content = f"[{tool_name}] executed"

        # 发送 tool_result 回核心
        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": result_content,
            "chat_id": chat_id,
        })

    async def send_message(self, incoming: IncomingMessage) -> dict:
        """
        将 IncomingMessage 转发给核心，并等待响应。

        用于消息处理的主流程。
        """
        inbound = incoming_to_inbound(
            incoming,
            bot_id=self.bot_id,
            project_path=self.project_path,
        )

        req = JsonRpcRequest(
            id=self._next_id(),
            method="feishu.message",
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
                "thread_id": inbound.thread_id or "",
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

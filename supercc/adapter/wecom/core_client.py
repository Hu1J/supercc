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
        self._pending_message_ids: dict[str, str] = {}  # req_id → message_id
        self._accumulator_by_msg_id: dict[str, _WeComStreamAccumulator] = {}
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

    class _WeComStreamAccumulator:
        """缓冲流式文本，1.5s idle flush + tool_call 时立即 flush。"""
        def __init__(self, chat_id: str, message_id: str, send_fn, flush_timeout: float = 1.5):
            self.chat_id = chat_id
            self._message_id = message_id
            self._send = send_fn
            self._flush_timeout = flush_timeout
            self._buffer = ""
            self._lock = asyncio.Lock()
            self._timer_task: asyncio.Task | None = None
            self.sent_something = False

        async def add_text(self, text: str) -> None:
            if not text:
                return
            async with self._lock:
                self._buffer += text
                if self._timer_task:
                    self._timer_task.cancel()
                self._timer_task = asyncio.create_task(self._flush_after(self._flush_timeout))

        async def flush(self) -> None:
            async with self._lock:
                if self._timer_task:
                    self._timer_task.cancel()
                    self._timer_task = None
                if self._buffer:
                    text = self._buffer
                    self._buffer = ""
                    if text.strip():
                        await self._send(self.chat_id, self._message_id, text)
                        self.sent_something = True

        async def _flush_after(self, delay: float) -> None:
            try:
                await asyncio.sleep(delay)
                async with self._lock:
                    if self._buffer:
                        text = self._buffer
                        self._buffer = ""
                        if text.strip():
                            await self._send(self.chat_id, self._message_id, text)
                            self.sent_something = True
            except asyncio.CancelledError:
                pass

    async def _handle_core_message(self, data: dict):
        if "id" in data:
            req_id = str(data.get("id"))
            msg_id = self._pending_message_ids.pop(req_id, None)
            if msg_id and msg_id in self._accumulator_by_msg_id:
                acc = self._accumulator_by_msg_id.pop(msg_id)
                await acc.flush()
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)

    async def _render_and_send(self, params: dict):
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")

        if not content:
            return

        if message_id:
            if message_id not in self._accumulator_by_msg_id:
                self._accumulator_by_msg_id[message_id] = self._WeComStreamAccumulator(
                    chat_id=chat_id,
                    message_id=message_id,
                    send_fn=lambda cid, mid, text: self._do_send_markdown(cid, mid, text),
                    flush_timeout=1.5,
                )
            await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            await self._do_send_markdown(chat_id, message_id, content)

    async def _do_send_markdown(self, chat_id: str, message_id: str, text: str) -> None:
        """实际发送 Markdown 到企业微信。"""
        try:
            await self.wecom.send_markdown(chat_id, text)
        except Exception as e:
            logger.warning(f"[WeComCore] send_markdown failed: {e}")

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：先 flush accumulator，再处理工具调用。"""
        tool_name = params.get("tool_name", "")
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")

        if message_id and message_id in self._accumulator_by_msg_id:
            await self._accumulator_by_msg_id[message_id].flush()

        result_content = f"[{tool_name}] 执行完成"

        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": result_content,
            "chat_id": chat_id,
        })

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心，并等待响应。"""
        inbound = incoming_to_inbound(msg, bot_id=self.bot_id, project_path=self.project_path)

        # 显示 typing 提示
        try:
            await self.wecom.send_typing_indicator(inbound.session_key.chat_id)
        except Exception:
            pass  # typing indicator 失败不影响主流程

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
        self._pending_message_ids[str(req.id)] = inbound.message_id
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

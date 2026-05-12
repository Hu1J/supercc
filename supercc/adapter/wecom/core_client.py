"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable
from wecom_aibot_sdk import generate_req_id

logger = logging.getLogger(__name__)


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to WeCom in batches."""

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


class WeComCoreWSClient:
    """
    企业微信插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 WeCom 消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，通过 SDK 的 reply_stream 渲染为企业微信格式并发送

    不负责：Session 管理、AI 推理
    """

    def __init__(
        self,
        core_url: str,
        ws_client,          # WeComWSClient (SDK-based)
        wecom_client: WeComClient,
        bot_id: str,
        project_path: str,
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.ws_client = ws_client       # SDK WSClient
        self.wecom = wecom_client        # WeComClient (消息发送)
        self.bot_id = bot_id
        self.project_path = project_path
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        # req_id → (message_id, chat_id)
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0
        self._sent_message_ids: set[str] = set()       # 幂等性（主动发送去重）
        # Stream accumulators keyed by message_id (for buffering streaming chunks)
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        # 群聊历史：chat_id → 最近10条消息（内存滚动存储）
        self._group_history: dict[str, list[dict]] = {}
        self._MAX_GROUP_HISTORY = 10

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
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
                # 流结束，清理 ws_client 中缓存的 frame
                self.ws_client.pop_frame(msg_id)
                # Flush and clean up the stream accumulator for this message
                if msg_id in self._accumulator_by_msg_id:
                    acc = self._accumulator_by_msg_id.pop(msg_id)
                    await acc.flush()
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE:
            await self._render_and_send(params)
        elif method == Event.STREAM_CHUNK:
            # 流式输出中 - use accumulator for buffering
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == Event.PONG:
            pass  # 心跳响应

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为企业微信格式并发送。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")

        if not content:
            return

        # Buffer text chunks for efficient batched sending
        if message_id:
            if message_id not in self._accumulator_by_msg_id:
                self._accumulator_by_msg_id[message_id] = StreamAccumulator(
                    chat_id=chat_id,
                    message_id=message_id,
                    send_fn=lambda cid, mid, text: self._do_send_text(cid, text, mid),
                )
            await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            # No message_id (e.g. final RESPONSE without streaming) - send directly
            await self.wecom.send_markdown(chat_id, content)

    async def _do_send_text(self, chat_id: str, text: str, message_id: str) -> None:
        """Send text to WeCom (called by StreamAccumulator after buffering)."""
        if message_id:
            frame = self.ws_client.get_frame(message_id)
            stream_id = generate_req_id("stream")
            try:
                if frame:
                    await self.ws_client.reply_stream(
                        frame=frame,
                        stream_id=stream_id,
                        content=text,
                        finish=True,
                    )
                else:
                    await self.wecom.send_markdown(chat_id, text)
            except Exception as e:
                logger.warning(f"[WeComCore] reply_stream failed: {e}")
                try:
                    await self.wecom.send_markdown(chat_id, text)
                except Exception:
                    pass
        else:
            await self.wecom.send_markdown(chat_id, text)

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：发送工具执行结果。"""
        tool_name = params.get("tool_name", "")
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # Flush any pending streaming text for this message before handling tool call
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        result_content = f"[{tool_name}] 执行完成"
        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": result_content,
            "chat_id": chat_id,
        })

    async def _check_group_permissions(self, inbound) -> bool:
        """检查群聊权限。返回 True=允许通过，False=已拦截（已发送授权卡片）。"""
        is_group = inbound.extra.get("is_group_chat", False)

        if is_group:
            entry = self._groups.get(inbound.session_key.chat_id)
            if entry is None:
                reason = "该群未配置使用权限，请联系管理员。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            if not getattr(entry, "enabled", True):
                reason = "该群已被禁用。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                reason = "请 @CC 我来使用 SuperCC。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            allow_from = getattr(entry, "allow_from", [])
            if allow_from and inbound.user_open_id not in allow_from:
                reason = "你在该群中没有使用权限。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            return True
        else:
            if self._allowed_users and inbound.user_open_id not in self._allowed_users:
                reason = "你不在允许使用列表中。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False
            return True

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心，并等待响应。"""
        inbound = incoming_to_inbound(msg, bot_id=self.bot_id, project_path=self.project_path)

        # 群聊权限校验
        if not await self._check_group_permissions(inbound):
            return {}

        # ── 群聊非@mention消息：只存内存，通知core更新session ─────────────────
        if inbound.extra.get("is_group_chat") and not inbound.extra.get("mention_bot"):
            hist = self._group_history.setdefault(inbound.session_key.chat_id, [])
            hist.append(msg)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist[:] = hist[-self._MAX_GROUP_HISTORY:]
            # 发轻量通知让 core 更新 session（不计消息数，避免触发 AI 处理）
            try:
                notify_req = JsonRpcRequest(
                    id=self._next_id(),
                    method="wecom.notify",
                    params={
                        "chat_id": inbound.session_key.chat_id,
                        "user_open_id": inbound.user_open_id or "",
                        "platform": inbound.session_key.platform,
                        "project_path": inbound.session_key.project_path,
                        "content": inbound.content,
                    },
                )
                await self._ws.send(json.dumps(notify_req.to_dict()))
            except Exception:
                pass
            logger.info(f"[WeComCore] group msg stored, hist_len={len(hist)}")
            return {}

        # ── 群聊上下文 enrichment（历史、成员列表、引用消息）───────────────
        await self._enrich_group_context(inbound, msg)

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
        self._pending_message_ids[str(req.id)] = (inbound.message_id, inbound.session_key.chat_id)
        await self._ws.send(json.dumps(req.to_dict()))
        result = await future
        return result or {}

    async def _enrich_group_context(self, inbound, msg: dict):
        """为群聊消息收集并注入上下文：历史。

        历史从内存中取（WeCom API 能力有限）。
        """
        if not inbound.extra.get("is_group_chat"):
            return

        chat_id = inbound.session_key.chat_id
        extra = inbound.extra

        # 群历史（从内存）
        hist = self._group_history.get(chat_id, [])
        if hist:
            history_lines = []
            for h in hist[-10:]:
                sender = h.get("sender", {}).get("id", "?")
                content = h.get("content", "") or h.get("body", {}).get("content", "")
                if content:
                    history_lines.append(f"{sender}: {content[:200]}")
            extra["group_history"] = history_lines

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

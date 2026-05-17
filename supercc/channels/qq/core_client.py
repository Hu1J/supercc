"""QQ channel plugin Thin Client: connects to core WebSocket service.

Responsibilities:
- Connect to core WS service
- Bridge QQ messages (from QQWSClient) to core
- Bridge core responses back to QQ via QQClient
- Handle tool_call events
- Handle streaming via StreamAccumulator

Does NOT manage: Session management, AI inference
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any, Optional

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.channels.qq.client import IncomingQQMessage
from supercc.channels.qq.format.reply_formatter import QQReplyFormatter
from supercc.channels.common.format import MemoryCardMarker

logger = logging.getLogger("qq")


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to QQ in batches."""

    def __init__(self, chat_id: str, message_id: str, send_fn, flush_timeout: float = 1.5):
        self.chat_id = chat_id
        self._message_id = message_id
        self._send = send_fn
        self._flush_timeout = flush_timeout
        self._buffer = ""
        self._lock = asyncio.Lock()
        self._timer_task: Optional[asyncio.Task] = None
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


class QQCoreWSClient:
    """
    QQ channel plugin Thin Client.

    Responsibilities:
    - Connect to core WS service
    - Convert IncomingQQMessage → core, receive core events
    - Render core events → QQ format and send via QQClient
    - Handle tool_call events (forward to QQReplyFormatter)
    """

    def __init__(
        self,
        core_url: str,
        qq_client,      # QQClient instance (for sending)
        bot_openid: str,
        project_path: str,
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.qq = qq_client
        self.bot_openid = bot_openid
        self.project_path = project_path
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._reconnect_lock = asyncio.Lock()
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, tuple[str, str]] = {}  # req_id → (msg_id, chat_id)
        self._id_counter = 0
        self._streamed_msg_ids: set[str] = set()
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        self._last_chat_id: str = ""
        self._last_message_id: str = ""

        self.formatter = QQReplyFormatter()

        # Memory Manager
        try:
            from supercc.core.claude.memory_manager import get_memory_manager
            self._memory_manager = get_memory_manager()
        except Exception:
            self._memory_manager = None

    async def _send_auth(self):
        """Send auth message to core for identity verification."""
        from supercc.config import get_config
        cfg = get_config()
        if cfg.core.token:
            await self._ws.send(json.dumps({
                "type": "auth",
                "token": cfg.core.token,
                "platform": "qq"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "qq"
            }))

    async def connect(self):
        """Connect to core WebSocket service."""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[QQCore] Connected to core at %s", self.core_url)
        await self._send_auth()
        asyncio.create_task(self._read_loop())
        asyncio.create_task(self._ping_loop())

    async def _reconnect(self, jitter: bool = True):
        """Reconnect to core WS with backoff."""
        import websockets
        if jitter:
            await asyncio.sleep(random.uniform(0, 3))
        async with self._reconnect_lock:
            if self._ws:
                try:
                    if self._ws.close_code is None:
                        return
                except Exception:
                    pass
            old_ws = self._ws
            self._ws = None
            if old_ws:
                try:
                    await old_ws.close()
                except Exception:
                    pass
            self._ws = await websockets.connect(self.core_url)
            logger.info("[QQCore] Reconnected to core")
            await self._send_auth()

    async def _read_loop(self):
        """Read messages from core, reconnecting on errors."""
        import websockets
        while self._running:
            ws = self._ws
            if ws is None:
                await asyncio.sleep(1)
                continue
            try:
                msg = await ws.recv()
                data = json.loads(msg)
                await self._handle_core_message(data)
            except websockets.exceptions.ConnectionClosed:
                if self._running:
                    logger.warning("[QQCore] Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("[QQCore] Error reading message\n%s", traceback.format_exc())

    async def _ping_loop(self):
        """Ping core to detect connection health."""
        import websockets
        while self._running:
            await asyncio.sleep(30)
            ws = self._ws
            if ws is None:
                continue
            future = asyncio.Future()
            req_id = str(self._next_id())
            self._pending_responses[req_id] = future
            try:
                await ws.send(json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "qq"}
                ))
                await asyncio.wait_for(future, timeout=60)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                logger.warning("[QQCore] ping timeout, reconnecting...")
                self._pending_responses.pop(req_id, None)
                await self._reconnect(jitter=False)
            except Exception:
                self._pending_responses.pop(req_id, None)
            else:
                self._pending_responses.pop(req_id, None)

    async def _handle_core_message(self, data: dict):
        """Handle core events and responses."""
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
                # Flush streaming accumulator
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
            extra = params.get("extra", {})
            content = params.get("content", "")
            is_group = extra.get("is_group_chat", False)
            sender_id = extra.get("user_open_id", "")

            # Group: append @sender if not already mentioned
            if is_group and sender_id:
                if f"@{sender_id}" not in content:
                    params["content"] = content + f"@{sender_id}"

            await self._render_and_send(params)
            session_info = extra.get("session_info", "")
            if session_info:
                chat_id = params.get("chat_id", "")
                await self.qq.send_group_text(chat_id, session_info) if is_group else await self.qq.send_c2c_text(chat_id, session_info)

        elif method == Event.STREAM_CHUNK:
            await self._render_and_send(params)

        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)

        elif method == "restart":
            chat_id = params.get("chat_id", "")
            msg_id = params.get("message_id", "")
            content = params.get("content", "正在重启...")
            if content:
                await self.qq.send_group_text(chat_id, content) if params.get("extra", {}).get("is_group_chat") else await self.qq.send_c2c_text(chat_id, content)

        elif method == "command_progress":
            await self._handle_command_progress(params)

        elif method == "cron_progress":
            await self._handle_cron_progress(params)

        elif method == "cron_result":
            await self._handle_cron_result(params)

        elif method == Event.NOTIFICATION:
            chat_id = params.get("chat_id", "")
            content = params.get("content", "")
            if content:
                is_group = params.get("extra", {}).get("is_group_chat", False)
                await self.qq.send_group_text(chat_id, content) if is_group else await self.qq.send_c2c_text(chat_id, content)

    async def _render_and_send(self, params: dict):
        """Render OutboundMessage as QQ format and send."""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        # Non-streaming events (restart/update) bypass accumulator
        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            await self.qq.send_group_text(chat_id, content)
            return

        is_group = params.get("extra", {}).get("is_group_chat", False)

        if message_id:
            if message_id in self._accumulator_by_msg_id:
                self._streamed_msg_ids.add(message_id)
                acc = self._accumulator_by_msg_id[message_id]
                if event == Event.RESPONSE:
                    await acc.flush()
                    del self._accumulator_by_msg_id[message_id]
                else:
                    await acc.add_text(content)
            elif message_id:
                self._streamed_msg_ids.add(message_id)
                self._accumulator_by_msg_id[message_id] = StreamAccumulator(
                    chat_id=chat_id,
                    message_id=message_id,
                    send_fn=lambda cid, mid, text: self._do_send_text(cid, text, mid, is_group),
                )
                await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            await self._do_send_text(chat_id, content, message_id, is_group)

    async def _do_send_text(self, chat_id: str, text: str, message_id: str, is_group: bool) -> None:
        """Send text to QQ."""
        try:
            if is_group:
                await self.qq.send_group_text(chat_id, text, reply_to=message_id)
            else:
                await self.qq.send_c2c_text(chat_id, text, reply_to=message_id)
        except Exception as e:
            logger.warning("[QQCore] send failed: %s", e)

    async def _handle_command_progress(self, params: dict):
        """Handle restart/update progress notifications."""
        event = params.get("event", "")
        step = params.get("step", 0)
        total = params.get("total", 0)
        status = params.get("status", "")
        detail = params.get("detail", "")
        new_pid = params.get("new_pid")

        chat_id = self._last_chat_id or ""
        if not chat_id:
            return

        bar = "▓" * step + "░" * (total - step)

        if status == "final":
            if event == "restart":
                body = f"新进程 PID: {new_pid}\n\nSuperCC 已重启，可以在 QQ 中继续对话了。"
            elif event == "update":
                body = "SuperCC 已更新，可以在 QQ 中继续对话了。"
            else:
                body = detail
            text = f"✅ 完成\n\n{body}"
        else:
            labels = {
                "restart": ["🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
                "update":  ["📋 检查更新", "📦 检查新版本", "✅ 下载完成", "🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 更新完成"],
            }
            label_list = labels.get(event, [])
            step_label = label_list[step - 1] if step <= len(label_list) else f"步骤 {step}"
            text = f"⏳ {step_label}\n\n{bar} {step}/{total}\n\n{event} 中，请稍候..."

        try:
            is_group = params.get("extra", {}).get("is_group_chat", False)
            if is_group:
                await self.qq.send_group_text(chat_id, text)
            else:
                await self.qq.send_c2c_text(chat_id, text)
        except Exception as e:
            logger.warning("[QQCore] command_progress send failed: %s", e)

    async def _handle_cron_progress(self, params: dict):
        """Handle cron progress notifications."""
        content = params.get("content", "")
        chat_id = params.get("chat_id") or self._last_chat_id or ""
        if not chat_id:
            return
        if not isinstance(content, str):
            content = str(content)
        try:
            is_group = params.get("extra", {}).get("is_group_chat", False)
            if is_group:
                await self.qq.send_group_text(chat_id, content)
            else:
                await self.qq.send_c2c_text(chat_id, content)
        except Exception as e:
            logger.warning("[QQCore] cron_progress failed: %s", e)

    async def _handle_cron_result(self, params: dict):
        """Handle cron final result."""
        job_name = params.get("job_name", "")
        content = params.get("content", "")
        error = params.get("error")
        chat_id = params.get("chat_id") or self._last_chat_id or ""
        if not chat_id:
            return
        if not isinstance(content, str):
            content = str(content)
        try:
            if error:
                text = f"⏰ **{job_name}**\n\n❌ 错误: {error}"
            else:
                text = f"⏰ **{job_name}**\n\n{content}"
            is_group = params.get("extra", {}).get("is_group_chat", False)
            if is_group:
                await self.qq.send_group_text(chat_id, text)
            else:
                await self.qq.send_c2c_text(chat_id, text)
        except Exception as e:
            logger.warning("[QQCore] cron_result failed: %s", e)

    async def _handle_tool_call(self, params: dict):
        """Handle tool call event from core."""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")
        is_group = extra.get("is_group_chat", False)

        # Flush any pending streaming for this message
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        kwargs = {}
        if tool_name.startswith("mcp__SuperCC__Memory") and self._memory_manager:
            kwargs["memory_manager"] = self._memory_manager
            kwargs["default_project_path"] = self.project_path
            kwargs["platform"] = "qq"
            kwargs["chat_id"] = chat_id

        result = self.formatter.format_tool_call(tool_name, tool_input, **kwargs)

        if isinstance(result, MemoryCardMarker):
            text = result.render()
        elif isinstance(result, str):
            text = result
        else:
            text = f"🤖 **{tool_name}**"

        if text:
            try:
                if is_group:
                    await self.qq.send_group_text(chat_id, text, reply_to=msg_id)
                else:
                    await self.qq.send_c2c_text(chat_id, text, reply_to=msg_id)
            except Exception as e:
                logger.warning("[QQCore] tool_call send failed: %s", e)

    async def _check_group_permissions(self, incoming: IncomingQQMessage) -> bool:
        """Check group chat permissions. Returns True=allow, False=block."""
        if not incoming.group_openid:
            return True

        entry = self._groups.get(incoming.group_openid)
        if entry is None:
            return True  # Unknown group, allow

        if not getattr(entry, "enabled", True):
            return False

        # Check if mention is required
        if getattr(entry, "require_mention", True):
            # For group messages, check if bot was mentioned
            if not self._was_mentioned(incoming.content):
                return False

        allow_from = getattr(entry, "allow_from", [])
        if allow_from and incoming.author_id not in allow_from:
            return False

        return True

    def _was_mentioned(self, content: str) -> bool:
        """Check if the bot was mentioned in the message content."""
        # QQ group @mention format: <@!user_id> or just @user_id
        import re
        # Look for <@!OPENID> or @OPENID patterns
        return bool(re.search(r"<@!" + self.bot_openid + r">", content)) or \
               bool(re.search(r"@" + self.bot_openid + r"\b", content))

    def _was_mentioned_raw(self, data: dict) -> bool:
        """Check if bot was mentioned using raw message data."""
        mentions = data.get("mentions", []) or data.get("mention_users", []) or []
        for m in mentions:
            if isinstance(m, dict):
                uid = m.get("id", "") or m.get("user_id", "")
                if uid == self.bot_openid:
                    return True
            elif isinstance(m, str) and m == self.bot_openid:
                return True
        return False

    async def send_message(self, incoming: IncomingQQMessage) -> dict:
        """
        Bridge IncomingQQMessage to core WS service and wait for response.

        Handles:
        - Message parsing and @mention stripping
        - Group permissions check
        - Streaming via StreamAccumulator
        """
        import websockets

        # Check group permissions for group messages
        if incoming.group_openid:
            if not await self._check_group_permissions(incoming):
                logger.info("[QQCore] Group message blocked by permissions: %s", incoming.group_openid)
                return {}

        # Save chat context for command_progress
        self._last_chat_id = incoming.group_openid or incoming.author_id
        self._last_message_id = incoming.message_id

        # Strip @mention prefix from group messages
        content = incoming.content
        if incoming.group_openid and self._was_mentioned(content):
            import re
            # Strip <@!OPENID> and @OPENID patterns
            content = re.sub(r"<@!" + self.bot_openid + r">\s*", "", content)
            content = re.sub(r"@" + self.bot_openid + r"\b\s*", "", content, count=1)
            content = content.strip()

        # Determine if this is a group message
        is_group = bool(incoming.group_openid)
        chat_id = incoming.group_openid or incoming.author_id

        # Build request to core
        req = JsonRpcRequest(
            id=self._next_id(),
            method="qq.message",
            platform="qq",
            params={
                "message_id": incoming.message_id,
                "bot_id": self.bot_openid,
                "chat_id": chat_id,
                "user_open_id": incoming.author_id,
                "project_path": self.project_path,
                "content": content,
                "message_type": incoming.message_type,
                "is_group_chat": is_group,
                "mention_bot": self._was_mentioned(incoming.content) or self._was_mentioned_raw(incoming.raw),
                "group_name": incoming.group_name or "",
                "extra": {
                    "author_name": incoming.author_name,
                    "group_openid": incoming.group_openid,
                },
            },
        )

        future = asyncio.Future()
        req_id = str(req.id)
        self._pending_responses[req_id] = future
        self._pending_message_ids[req_id] = (incoming.message_id, chat_id)

        try:
            await self._ws.send(json.dumps(req.to_dict()))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("[QQCore] send failed, reconnecting...")
            await self._reconnect(jitter=False)
            self._pending_responses[req_id] = future
            self._pending_message_ids[req_id] = (incoming.message_id, chat_id)
            await self._ws.send(json.dumps(req.to_dict()))

        try:
            result = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            logger.warning("[QQCore] response timeout")
            result = {}
        except Exception:
            logger.error("[QQCore] send_message error\n%s", traceback.format_exc())
            result = {}
        return result or {}

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """Close the connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
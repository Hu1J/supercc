"""钉钉插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any

from supercc.core.protocol import JsonRpcRequest, Event

logger = logging.getLogger(__name__)


class DingTalkReplyFormatter:
    """Format tool call results for DingTalk (limited markdown support)."""

    ICONS = {
        "Read": "📖",
        "Write": "✏️",
        "Edit": "🔧",
        "Bash": "💻",
        "Glob": "🔍",
        "Grep": "🔎",
        "WebFetch": "🌐",
        "WebSearch": "🌐",
        "Task": "📋",
        "TodoWrite": "📋",
        "MemorySearch": "🧠",
        "MemoryList": "🧠",
        "MemoryAdd": "🧠",
        "MemoryDelete": "🧠",
        "AskUserQuestion": "🎯",
        "SkillSearch": "🎯",
        "CronCreate": "⏰",
        "CronDelete": "⏰",
        "CronList": "⏰",
        "CronPause": "⏰",
        "CronResume": "⏰",
        "CronTrigger": "⏰",
        "CronLogs": "⏰",
    }

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "dingtalk",
        chat_id: str = "",
    ) -> str:
        """Format a tool call notification as markdown text."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit → diff markdown
        if tool_name == "Edit":
            return self._format_edit_tool(tool_input, icon)

        # Write → diff markdown
        if tool_name == "Write":
            return self._format_write_tool(tool_input, icon)

        # Bash → code block
        if tool_name == "Bash":
            return self._format_bash_tool(tool_input, icon)

        # TodoWrite → markdown table
        if tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input, icon)

        # Read → file path
        if tool_name == "Read":
            return self._format_read_tool(tool_input, icon)

        # Memory MCP tools → simple text
        if tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            return self._format_memory_tool(tool_name, tool_input, icon)

        # Default: icon + name + first 100 chars of input
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    def _format_edit_tool(self, tool_input: str, icon: str) -> str:
        """Format Edit tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            file_path = data.get("file_path", "unknown")
            return f"{icon} **Edit** — `{file_path}`"
        except json.JSONDecodeError:
            return f"{icon} **Edit**"

    def _format_write_tool(self, tool_input: str, icon: str) -> str:
        """Format Write tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            file_path = data.get("file_path", "unknown")
            return f"{icon} **Write** — `{file_path}`"
        except json.JSONDecodeError:
            return f"{icon} **Write**"

    def _format_bash_tool(self, tool_input: str, icon: str) -> str:
        """Format Bash tool call as a markdown code block."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            cmd = data.get("command", tool_input)
            desc = data.get("description", "")
            header = f"{icon} **Bash**"
            if desc:
                header += f" — {desc}"
            return f"{header}\n```bash\n{cmd}\n```"
        except json.JSONDecodeError:
            return f"{icon} **Bash**\n```bash\n{tool_input}\n```"

    def _format_todowrite_tool(self, tool_input: str, icon: str) -> str:
        """Format TodoWrite tool call as a markdown table."""
        try:
            data = json.loads(tool_input)
            todos = data.get("todos", [])
        except json.JSONDecodeError:
            todos = []

        if not todos:
            return f"{icon} **TodoWrite** — 所有任务已完成"

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        rows = ["| 状态 | 待办事项 |", "|------|----------|"]
        for t in todos:
            icon_s = status_icon.get(t.get("status", "pending"), "⬜")
            content = str(t.get("content", "")).replace("\n", " ")
            rows.append(f"| {icon_s} | {content} |")
        return f"{icon} **TodoWrite**\n\n" + "\n".join(rows)

    def _format_read_tool(self, tool_input: str, icon: str) -> str:
        """Format Read tool call with backtick-wrapped file path."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            path = data.get("file_path", tool_input)
        except json.JSONDecodeError:
            path = tool_input
        return f"{icon} **Read** — `{path}`"

    def _format_memory_tool(self, tool_name: str, tool_input: str, icon: str) -> str:
        """Format Memory MCP tool call as simple text."""
        short = tool_name.replace("mcp__SuperCC__", "")
        msg = f"{icon} **{short}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to DingTalk in batches."""

    def __init__(self, chat_id: str, session_webhook: str, message_id: str, send_fn, flush_timeout: float = 1.5):
        self.chat_id = chat_id
        self.session_webhook = session_webhook
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
                    await self._send(self.session_webhook, self.chat_id, text)
                    self.sent_something = True

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._buffer:
                    text = self._buffer
                    self._buffer = ""
                    if text.strip():
                        await self._send(self.session_webhook, self.chat_id, text)
                        self.sent_something = True
        except asyncio.CancelledError:
            pass


class DingTalkCoreWSClient:
    """
    钉钉插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将钉钉消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，通过 session webhook 渲染为钉钉格式并发送

    不负责：Session 管理、AI 推理
    """

    def __init__(
        self,
        core_url: str,
        ws_client,          # DingTalkWSClient (SDK-based)
        dingtalk_client,    # DingTalkClient (消息发送)
        bot_id: str,
        project_path: str,
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.ws_client = ws_client       # DingTalkWSClient
        self.dingtalk = dingtalk_client   # DingTalkClient
        self.bot_id = bot_id
        self.project_path = project_path
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._id_counter = 0
        # 流式消息 ID 追踪
        self._streamed_msg_ids: set[str] = set()
        # Stream accumulators keyed by message_id
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        # 当前 chat 上下文
        self._last_chat_id: str = ""
        self._last_session_webhook: str = ""
        # 群聊历史
        self._group_history: dict[str, list[dict]] = {}
        self._MAX_GROUP_HISTORY = 10
        # Memory Manager
        try:
            from supercc.core.claude.memory_manager import get_memory_manager
            self._memory_manager = get_memory_manager()
        except Exception:
            self._memory_manager = None

        # 格式化器
        self.formatter = DingTalkReplyFormatter()
        # 重连互斥锁
        self._reconnect_lock = asyncio.Lock()

    async def _send_auth(self):
        """发送 auth 消息到核心，完成身份认证。"""
        from supercc.config import get_config
        cfg = get_config()
        if cfg.core.token:
            await self._ws.send(json.dumps({
                "type": "auth",
                "token": cfg.core.token,
                "platform": "dingtalk"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "dingtalk"
            }))

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[DingTalkCore] Connected to core")
        await self._send_auth()
        asyncio.create_task(self._read_loop())
        asyncio.create_task(self._ping_loop())

    async def _read_loop(self):
        """持续读取核心发来的消息。连接断开时自动重连。"""
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
                    logger.warning("[DingTalkCore] Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("[DingTalkCore] Error reading message\n%s", traceback.format_exc())

    async def _reconnect(self, jitter: bool = True):
        """断开旧连接，重新连接核心 WebSocket。"""
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
            logger.info("[DingTalkCore] Reconnected to core")
            await self._send_auth()

    async def _ping_loop(self):
        """定期 ping core，检测连接是否健康。"""
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
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "dingtalk"}
                ))
                await asyncio.wait_for(future, timeout=60)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                logger.warning("[DingTalkCore] ping timeout, reconnecting...")
                self._pending_responses.pop(req_id, None)
                await self._reconnect(jitter=False)
            except Exception:
                self._pending_responses.pop(req_id, None)
            else:
                self._pending_responses.pop(req_id, None)

    async def _handle_core_message(self, data: dict):
        if "id" in data:
            req_id = str(data.get("id"))
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE:
            await self._render_and_send(params)
        elif method == Event.STREAM_CHUNK:
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == "restart":
            chat_id = params.get("chat_id", "")
            content = params.get("content", "正在重启...")
            if content:
                await self.dingtalk.send_text(self._last_session_webhook, content, chat_id)
        elif method == Event.PONG:
            pass
        elif method == Event.NOTIFICATION:
            chat_id = params.get("chat_id", "")
            content = params.get("content", "")
            if content:
                await self.dingtalk.send_text(self._last_session_webhook, content, chat_id)

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为钉钉格式并发送。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        # 获取该 chat_id 对应的 session_webhook
        session_webhook = self._get_session_webhook(chat_id)
        if not session_webhook:
            logger.warning("[DingTalkCore] No session_webhook for chat_id=%s", chat_id[:20] if chat_id else "?")
            return

        # 非流式事件直接发送
        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            await self.dingtalk.send_text(session_webhook, content, chat_id)
            return

        # Buffer text chunks for streaming
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
                    session_webhook=session_webhook,
                    message_id=message_id,
                    send_fn=lambda sw, cid, text: self._do_send_text(sw, text, cid),
                )
                await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            await self.dingtalk.send_text(session_webhook, content, chat_id)

    async def _do_send_text(self, session_webhook: str, text: str, chat_id: str) -> None:
        """Send text to DingTalk via session webhook."""
        try:
            await self.dingtalk.send_text(session_webhook, text, chat_id)
        except Exception as e:
            logger.warning("[DingTalkCore] _do_send_text failed: %s", e)

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：格式化工具结果并发送。"""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        chat_id = params.get("chat_id", "")

        # Flush any pending streaming text
        msg_id = params.get("message_id", "")
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        session_webhook = self._get_session_webhook(chat_id)
        if not session_webhook:
            return

        result = self.formatter.format_tool_call(
            tool_name, tool_input,
            memory_manager=self._memory_manager,
            default_project_path=self.project_path,
            platform="dingtalk",
            chat_id=chat_id,
        )

        if result:
            try:
                await self.dingtalk.send_text(session_webhook, str(result), chat_id)
            except Exception as e:
                logger.warning("[DingTalkCore] _handle_tool_call send failed: %s", e)

    def _get_session_webhook(self, chat_id: str) -> str:
        """获取 chat_id 对应的 session webhook。"""
        # 这里需要维护一个 chat_id -> session_webhook 的映射
        # 实际上应该从 ws_client 传入的 msg 中获取
        # 暂时返回 last_session_webhook
        return self._last_session_webhook

    def store_session_webhook(self, chat_id: str, session_webhook: str) -> None:
        """存储 chat_id 对应的 session webhook。"""
        self._last_chat_id = chat_id
        self._last_session_webhook = session_webhook

    async def send_message(self, msg: dict) -> dict:
        """将钉钉消息转发给核心，并等待响应。"""
        chat_id = msg.get("chat_id", "")
        user_id = msg.get("user_id", "")
        user_nick = msg.get("user_nick", "")
        user_staff_id = msg.get("user_staff_id", "")
        content = msg.get("content", "")
        conversation_type = msg.get("conversation_type", "1")
        is_group = str(conversation_type) == "2"
        session_webhook = msg.get("session_webhook", "")
        msg_id = msg.get("msgid", "")
        robot_code = msg.get("robot_code", "")

        # 存储 session_webhook
        if session_webhook:
            self.store_session_webhook(chat_id, session_webhook)

        # 保存当前 chat 上下文
        self._last_chat_id = chat_id

        # 群聊权限检查
        if is_group:
            entry = self._groups.get(chat_id)
            if entry is None:
                logger.debug("[DingTalkCore] Group %s not in allowlist, ignoring", chat_id[:20] if chat_id else "?")
                return {}
            if not getattr(entry, "enabled", True):
                return {}

        # 检查用户白名单
        if self._allowed_users and "*" not in self._allowed_users:
            if user_staff_id and user_staff_id not in self._allowed_users:
                logger.debug("[DingTalkCore] User %s not in allowlist", user_staff_id)
                return {}

        # 群聊非@mention消息：只存内存，通知 core 更新 session
        if is_group and not content.lstrip().startswith("/"):
            hist = self._group_history.setdefault(chat_id, [])
            hist.append(msg)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist[:] = hist[-self._MAX_GROUP_HISTORY:]
            return {}

        # 构建 inbound message
        from supercc.channels.dingtalk.core_protocol import incoming_to_inbound
        inbound = incoming_to_inbound(
            msg,
            bot_id=self.bot_id,
            project_path=self.project_path,
            system_prompt="",
            group_members=None,
            group_context="",
        )

        # /restart 指令
        if inbound.content.strip() == "/restart":
            await self.dingtalk.send_text(session_webhook, "正在重启，请稍作等待...", chat_id)

        req = JsonRpcRequest(
            id=self._next_id(),
            method="dingtalk.message",
            platform="dingtalk",
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": user_staff_id or user_id,
                "project_path": inbound.session_key.project_path,
                "content": inbound.content,
                "message_type": inbound.message_type.value,
                "is_group_chat": is_group,
                "mention_bot": False,
                "mention_ids": [],
                "group_name": "",
                "extra": inbound.extra,
            },
        )

        logger.info(f"[DingTalkCore] send_message to core: req.id={req.id}, message_id={inbound.message_id[:20] if inbound.message_id else 'None'}")

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        await self._ws.send(json.dumps(req.to_dict()))
        result = await future
        return result or {}

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()

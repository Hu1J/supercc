"""微信个人（iLink）Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any, Optional

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.channels.wechat.lp_client import WeChatLongPollingClient
from supercc.channels.wechat.client import WeChatClient

logger = logging.getLogger(__name__)

# Item 类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

MAX_MESSAGE_LENGTH = 2000


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    """从 item_list 中提取文本内容。"""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    """判断是群聊还是私聊。"""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


class StreamAccumulator:
    """缓冲流式文本块，定期批量发送到微信。"""

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
                    await self._send(self.chat_id, text)
                    self.sent_something = True

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._buffer:
                    text = self._buffer
                    self._buffer = ""
                    if text.strip():
                        await self._send(self.chat_id, text)
                        self.sent_something = True
        except asyncio.CancelledError:
            pass


class WeChatReplyFormatter:
    """Format tool call results for WeChat (limited card support)."""

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
    ) -> str:
        """Format a tool call notification as markdown text."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "").replace("mcp__", "")

        # Bash → code block
        if tool_name == "Bash":
            try:
                data = json.loads(tool_input)
                cmd = data.get("command", tool_input)
                desc = data.get("description", "")
                header = f"{icon} **Bash**"
                if desc:
                    header += f" — {desc}"
                return f"{header}\n```bash\n{cmd}\n```"
            except json.JSONDecodeError:
                return f"{icon} **Bash**\n```bash\n{tool_input}\n```"

        # TodoWrite → markdown table
        if tool_name == "TodoWrite":
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

        # Read → file path
        if tool_name == "Read":
            try:
                data = json.loads(tool_input)
                path = data.get("file_path", tool_input)
            except json.JSONDecodeError:
                path = tool_input
            return f"{icon} **Read** — `{path}`"

        # Default: icon + name
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg


class WeChatCoreWSClient:
    """微信个人插件的 Thin Client。

    职责：
    - 通过 Long Polling 接收微信消息
    - 通过 WebSocket 连接核心服务
    - 将微信消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，通过 WeChatClient 发送
    """

    def __init__(
        self,
        core_url: str,
        token: str,
        account_id: str,
        bot_openid: str = "",
        data_dir: str = "",
        project_path: str = "",
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self._token = token
        self._account_id = account_id
        self._bot_openid = bot_openid
        self._data_dir = data_dir
        self._project_path = project_path
        self._allowed_users = allowed_users or []

        self._lp_client: Optional[WeChatLongPollingClient] = None
        self._client: Optional[WeChatClient] = None
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0

        # 流式消息追踪
        self._streamed_msg_ids: set[str] = set()
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}

        # 当前 chat 上下文
        self._last_chat_id: str = ""
        self._last_message_id: str = ""

        # Context token 存储（account_id:user_id -> context_token）
        self._context_tokens: dict[str, str] = {}

        # Typing ticket 缓存
        self._typing_tickets: dict[str, tuple[str, float]] = {}

        # 群聊历史：chat_id → 最近10条消息（内存滚动存储）
        self._group_history: dict[str, list[dict]] = {}
        self._MAX_GROUP_HISTORY = 10

        self.formatter = WeChatReplyFormatter()

    def _context_key(self, user_id: str) -> str:
        return f"{self._account_id}:{user_id}"

    def _get_context_token(self, user_id: str) -> Optional[str]:
        return self._context_tokens.get(self._context_key(user_id))

    def _set_context_token(self, user_id: str, token: str) -> None:
        self._context_tokens[self._context_key(user_id)] = token

    def _get_typing_ticket(self, user_id: str) -> Optional[str]:
        entry = self._typing_tickets.get(user_id)
        if not entry:
            return None
        if asyncio.get_event_loop().time() - entry[1] >= 600:
            self._typing_tickets.pop(user_id, None)
            return None
        return entry[0]

    def _set_typing_ticket(self, user_id: str, ticket: str) -> None:
        self._typing_tickets[user_id] = (ticket, asyncio.get_event_loop().time())

    def _enrich_group_context(self, chat_id: str, current_message_id: str) -> str:
        """从群聊历史构建上下文字符串。"""
        hist = self._group_history.get(chat_id, [])
        if not hist:
            return ""

        history_lines = []
        for h_msg in hist:
            h_msg_id = h_msg.get("message_id", "") if isinstance(h_msg, dict) else ""
            # 跳过当前消息，避免重复注入
            if h_msg_id == current_message_id:
                continue
            h_user_open_id = h_msg.get("user_open_id", "") if isinstance(h_msg, dict) else ""
            h_content = h_msg.get("content", "") if isinstance(h_msg, dict) else ""
            if h_content:
                history_lines.append(f"{h_user_open_id}: {h_content}")

        return "\n".join(history_lines)

    async def _send_auth(self) -> None:
        """发送 auth 消息到核心。"""
        from supercc.config import get_config
        cfg = get_config()
        if cfg.core.token:
            await self._ws.send(json.dumps({
                "type": "auth",
                "token": cfg.core.token,
                "platform": "wechat"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "wechat"
            }))

    async def connect(self) -> None:
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[WeChatCore] Connected to core")
        await self._send_auth()
        asyncio.create_task(self._read_loop())
        asyncio.create_task(self._ping_loop())

    async def _read_loop(self) -> None:
        """持续读取核心发来的消息。"""
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
                    logger.warning("[WeChatCore] Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("[WeChatCore] Error reading message\n%s", traceback.format_exc())

    async def _reconnect(self, jitter: bool = True) -> None:
        """断开旧连接，重新连接核心 WebSocket。"""
        import websockets
        if jitter:
            await asyncio.sleep(random.uniform(0, 3))
        old_ws = self._ws
        self._ws = None
        if old_ws:
            try:
                await old_ws.close()
            except Exception:
                pass
        self._ws = await websockets.connect(self.core_url)
        logger.info("[WeChatCore] Reconnected to core")
        await self._send_auth()

    async def _ping_loop(self) -> None:
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
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "wechat"}
                ))
                await asyncio.wait_for(future, timeout=60)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                logger.warning("[WeChatCore] ping timeout, reconnecting...")
                self._pending_responses.pop(req_id, None)
                await self._reconnect(jitter=False)
            except Exception:
                self._pending_responses.pop(req_id, None)
            else:
                self._pending_responses.pop(req_id, None)

    async def _handle_core_message(self, data: dict) -> None:
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
                # 流结束，清理 accumulator
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
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == "pong":
            pass
        elif method == Event.NOTIFICATION:
            chat_id = params.get("chat_id", "")
            content = params.get("content", "")
            if content and self._client:
                try:
                    await self._client.send_text(chat_id, content)
                except Exception as exc:
                    logger.warning("[WeChatCore] notification send failed: %s", exc)

    async def _render_and_send(self, params: dict) -> None:
        """渲染 OutboundMessage 并发送到微信。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        # 非流式事件不经过 accumulator
        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            if self._client:
                await self._client.send_text(chat_id, content)
            return

        # 流式处理
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
                    send_fn=lambda cid, text: self._do_send_text(cid, text),
                )
                await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            await self._do_send_text(chat_id, content)

    async def _do_send_text(self, chat_id: str, text: str) -> None:
        """发送文本到微信。"""
        if not self._client:
            return
        context_token = self._get_context_token(chat_id)
        try:
            await self._client.send_text(chat_id, text, context_token)
        except Exception as exc:
            logger.warning("[WeChatCore] send_text failed to %s: %s", chat_id[:8], exc)

    async def _handle_tool_call(self, params: dict) -> None:
        """tool_call 事件：格式化工具结果并发送。"""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # Flush pending streaming text
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        result = self.formatter.format_tool_call(tool_name, tool_input)
        if result and self._client:
            context_token = self._get_context_token(chat_id)
            try:
                await self._client.send_text(chat_id, result, context_token)
            except Exception as exc:
                logger.warning("[WeChatCore] tool_call send failed: %s", exc)

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def send_message(self, msg: dict) -> None:
        """处理微信消息（来自 Long Polling 回调）。"""
        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id:
            return
        if sender_id == self._account_id:
            return

        message_id = str(msg.get("message_id") or "").strip()
        context_token = str(msg.get("context_token") or "").strip()
        if context_token:
            self._set_context_token(sender_id, context_token)

        # 保存当前 chat 上下文
        self._last_chat_id = sender_id
        self._last_message_id = message_id

        # 提取文本
        item_list = msg.get("item_list") or []
        text = _extract_text(item_list)

        # 检测群聊
        chat_type, chat_id = _guess_chat_type(msg, self._account_id)
        is_group_chat = chat_type == "group"

        # ── 群聊所有消息：记录到 _group_history ────────────────────────────
        if is_group_chat:
            hist = self._group_history.setdefault(chat_id, [])
            # 构建简化消息对象用于历史记录
            hist_entry = {
                "message_id": message_id,
                "user_open_id": sender_id,
                "content": text,
                "message_type": "text",
                "raw_content": json.dumps(msg),
            }
            hist.append(hist_entry)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist.pop(0)

        # 权限检查
        if is_group_chat:
            pass  # 群聊暂不检查 allowlist
        elif not self._is_dm_allowed(sender_id):
            logger.info("[WeChatCore] user %s not in allowlist, skipping", sender_id[:8])
            return

        # 异步获取 typing ticket
        if self._client:
            asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token))

        # ── 构建 group_context ────────────────────────────────────────────
        group_context = ""
        if is_group_chat:
            group_context = self._enrich_group_context(chat_id, message_id)

        # 发送到核心
        req = JsonRpcRequest(
            id=self._next_id(),
            method="wechat.message",
            platform="wechat",
            params={
                "message_id": message_id,
                "chat_id": chat_id if is_group_chat else sender_id,
                "user_open_id": sender_id,
                "project_path": self._project_path,
                "content": text,
                "message_type": "text",
                "is_group_chat": is_group_chat,
                "mention_bot": False,
                "group_context": group_context,
            },
        )

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        self._pending_message_ids[str(req.id)] = (message_id, sender_id)
        await self._ws.send(json.dumps(req.to_dict()))

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """检查私聊权限。"""
        if not self._allowed_users:
            return True
        return sender_id in self._allowed_users

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        """获取 typing ticket 并缓存。"""
        if not self._client or self._get_typing_ticket(user_id):
            return
        try:
            resp = await self._client.get_config(user_id, context_token)
            ticket = str(resp.get("typing_ticket") or "")
            if ticket:
                self._set_typing_ticket(user_id, ticket)
        except Exception as exc:
            logger.debug("[WeChatCore] getConfig failed for %s: %s", user_id[:8], exc)

    async def start(self) -> None:
        """启动 Long Polling 和 WebSocket 连接。"""
        self._lp_client = WeChatLongPollingClient(
            token=self._token,
            data_dir=self._data_dir,
        )
        self._client = WeChatClient(
            token=self._token,
            bot_openid=self._bot_openid,
        )

        # Long Polling 消息回调
        async def on_message(msg: dict) -> None:
            await self.send_message(msg)

        # 启动 Long Polling 循环
        asyncio.create_task(self._lp_client.run_loop(on_message))
        logger.info("[WeChatCore] Long polling started")

    async def close(self) -> None:
        """关闭所有连接。"""
        self._running = False
        if self._lp_client:
            await self._lp_client.close()
        if self._client:
            await self._client.close()
        if self._ws:
            await self._ws.close()
        logger.info("[WeChatCore] Closed")
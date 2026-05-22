"""微信个人（iLink）Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
    # 图片/视频/文件没有文本内容时，给个占位符（仅非图片类型）
    # 图片消息的 content 不需要占位符，media_path 已经携带路径
    for item in item_list:
        t = item.get("type")
        if t in {ITEM_VIDEO, ITEM_FILE}:
            return "[文件]"
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
        bot_id: str = "",
        bot_openid: str = "",
        data_dir: str = "",
        project_path: str = "",
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self._token = token
        self._account_id = account_id
        self._bot_id = bot_id or account_id
        self._bot_openid = bot_openid
        self._data_dir = data_dir
        self._project_path = project_path

        self._lp_client: Optional[WeChatLongPollingClient] = None
        self._client: Optional[WeChatClient] = None
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0

        # 流式消息追踪
        self._streamed_msg_ids: set[str] = set()

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
                logger.debug("[WeChatCore] WS recv: method=%s id=%s", data.get("method", ""), data.get("id", ""))
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
        method = data.get("method", "")
        # TOOL_CALL 是通知请求，带 id 但不走 response 路径
        if method == Event.TOOL_CALL:
            await self._handle_tool_call(data.get("params", {}))
            return

        if "id" in data:
            req_id = str(data.get("id"))
            logger.debug("[WeChatCore] ← WS response id=%s result=%s", req_id, str(data.get("result", ""))[:80])
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
                if msg_id not in self._streamed_msg_ids:
                    # 非流式响应（命令等）：直接从 response 发送
                    result = data.get("result") or {}
                    content = str(result.get("content", ""))
                    if content:
                        self._streamed_msg_ids.add(msg_id)
                        await self._do_send_text(chat_id, content)
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

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
        """渲染 OutboundMessage 并发送到微信。直接发送，不缓冲。

        STREAM_CHUNK：直接发送，不参与 _streamed_msg_ids 去重。
        RESPONSE：用 _streamed_msg_ids 防止与 JSON-RPC result 路径重复发送。
        """
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            if self._client:
                await self._client.send_text(chat_id, content)
            return

        if message_id:
            # RESPONSE：_streamed_msg_ids 去重（已有 STREAM_CHUNK 发出则跳过）
            if event == Event.RESPONSE:
                if message_id in self._streamed_msg_ids:
                    return
                self._streamed_msg_ids.add(message_id)
            else:
                # STREAM_CHUNK：直接发送，并标记（供 RESPONSE 去重）
                self._streamed_msg_ids.add(message_id)
            await self._do_send_text(chat_id, content)
        else:
            await self._do_send_text(chat_id, content)

    async def _do_send_text(self, chat_id: str, text: str) -> None:
        """发送文本到微信。"""
        if not self._client:
            return
        context_token = self._get_context_token(chat_id)
        try:
            await self._client.send_text(chat_id, text, context_token)
            logger.info("[WeChatCore] send_text OK to %s len=%d", chat_id[:12], len(text))
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

        logger.debug("[WeChatCore] tool_call: tool=%s chat_id=%s msg_id=%s", tool_name, chat_id[:8] if chat_id else "", msg_id[:8] if msg_id else "")

        result = self.formatter.format_tool_call(tool_name, tool_input)
        logger.debug("[WeChatCore] tool_call result: len=%d client=%s", len(result) if result else 0, self._client is not None)
        if result and self._client:
            context_token = self._get_context_token(chat_id)
            try:
                await self._client.send_text(chat_id, result, context_token)
                logger.debug("[WeChatCore] tool_call sent OK to %s", chat_id[:12])
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

        logger.info("[WeChatCore] ← received msg: %s", json.dumps(msg, ensure_ascii=False))

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

        # 检测消息类型并下载媒体
        media_path: str | None = None
        message_type_str = "text"
        if item_list:
            first_item = item_list[0]
            first_type = first_item.get("type")
            if first_type == ITEM_IMAGE:
                message_type_str = "image"
                if self._client:
                    media = first_item.get("image_item", {}).get("media", {})
                    eqp = media.get("encrypt_query_param", "")
                    aes_key = first_item.get("image_item", {}).get("aeskey", "")
                    full_url = media.get("full_url", "")
                    if eqp or full_url:
                        try:
                            media_path = await self._client.download_media_to_file(
                                eqp, aes_key or None, full_url or None, suffix=".jpg",
                                save_dir=self._data_dir, sub_dir="received_images",
                            )
                            logger.info("[WeChatCore] downloaded image: %s", media_path)
                            if media_path:
                                text = f"![image]({media_path})"
                        except Exception as exc:
                            logger.warning("[WeChatCore] image download failed: %s", exc)
            elif first_type in {ITEM_VIDEO, ITEM_FILE}:
                message_type_str = "file"
                if self._client:
                    media = first_item.get("file_item", {}).get("media", {})
                    eqp = media.get("encrypt_query_param", "")
                    aes_key_b64 = media.get("aes_key", "")
                    full_url = media.get("full_url", "")
                    raw_name = first_item.get("file_item", {}).get("file_name", "file")
                    # aes_key 是 base64 编码的 hex 字符串
                    # base64.b64decode → hex 字符串的 ASCII 字节 → .decode("ascii") → hex 字符串
                    aes_key_hex: Optional[str] = None
                    if aes_key_b64:
                        import base64 as _b64
                        aes_key_hex = _b64.b64decode(aes_key_b64).decode("ascii")
                    if eqp or full_url:
                        try:
                            suffix = os.path.splitext(raw_name)[1] or ".bin"
                            media_path = await self._client.download_media_to_file(
                                eqp, aes_key_hex, full_url or None, suffix=suffix,
                                save_dir=self._data_dir,
                            )
                            logger.info("[WeChatCore] downloaded file: %s", media_path)
                            if media_path:
                                text = f"![file]({media_path})"
                        except Exception as exc:
                            logger.warning("[WeChatCore] file download failed: %s", exc)

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

        # 权限检查（每次重新加载 config，支持 pairing approve 后实时生效）
        if not is_group_chat:
            from supercc.config import reload_config
            cfg = reload_config()
            channel_cfg = getattr(cfg.channels, "wechat", None)
            allowed_users = list(getattr(channel_cfg, "allowed_users", [])) if channel_cfg else []
            if sender_id not in allowed_users:
                logger.info("[WeChatCore] user %s not in allowlist", sender_id[:8])
                if self._client:
                    try:
                        from supercc.core.pairing import get_pairing_store
                        store = get_pairing_store()
                        code = store.generate_code("wechat", sender_id, "")
                        if code:
                            reason = (
                                f"你不在允许使用列表中。\n\n"
                                f"请联系管理员执行以下命令以获得使用权：\n\n"
                                f"supercc pairing approve {code}"
                            )
                        else:
                            reason = "你不在允许使用列表中。\n\n配对码已生成，请联系管理员执行 approve。"
                    except Exception:
                        reason = "你不在允许使用列表中。\n\n配对系统暂时不可用，请联系机器人所有者。"
                    context_token = self._get_context_token(sender_id)
                    asyncio.create_task(
                        self._client.send_text(sender_id, reason, context_token)
                    )
                return

        # 异步获取 typing ticket
        if self._client:
            asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token))

        # ── 构建 group_context ────────────────────────────────────────────
        group_context = ""
        if is_group_chat:
            group_context = self._enrich_group_context(chat_id, message_id)

        # 发送到核心
        logger.debug("[WeChatCore] → sending to core: message_type=%s, media_path=%s, content=%s",
                     message_type_str, media_path, text[:80] if text else "")
        req = JsonRpcRequest(
            id=self._next_id(),
            method="wechat.message",
            platform="wechat",
            params={
                "message_id": message_id,
                "bot_id": self._bot_id,
                "chat_id": chat_id if is_group_chat else sender_id,
                "user_open_id": sender_id,
                "project_path": self._project_path,
                "content": text,
                "message_type": message_type_str,
                "is_group_chat": is_group_chat,
                "mention_bot": False,
                "group_context": group_context,
                "media_path": media_path,
            },
        )

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        self._pending_message_ids[str(req.id)] = (message_id, sender_id)
        logger.debug("[WeChatCore] → to core: id=%s method=%s msg_id=%s", req.id, req.method, message_id)
        await self._ws.send(json.dumps(req.to_dict()))


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
"""飞书插件的 Thin Client：连接核心 WebSocket 服务。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from supercc.core.protocol import (
    JsonRpcRequest,
    OutboundMessage, Event,
)
from supercc.adapter.feishu.client import IncomingMessage
from supercc.adapter.feishu.core_protocol import incoming_to_inbound
from supercc.adapter.feishu.format.reply_formatter import ReplyFormatter, should_use_card
from supercc.adapter.feishu.format.questionnaire_card import format_questionnaire_card
from supercc.adapter.feishu.format.edit_diff import _DiffMarker, _MemoryCardMarker
from dataclasses import replace as dataclass_replace

logger = logging.getLogger("feishu")


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to Feishu in batches.

    Feishu message updates are expensive (one API call per message), so we buffer
    chunks and flush when a tool call arrives or after a short idle period.
    """

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
        """Append text chunk and (re)start the flush timer."""
        if not text:
            return
        async with self._lock:
            self._buffer += text
            if self._timer_task:
                self._timer_task.cancel()
            self._timer_task = asyncio.create_task(self._flush_after(self._flush_timeout))

    async def flush(self) -> None:
        """Send accumulated text immediately."""
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
        """Flush after a delay, but cancel if more text arrives."""
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


class FeishuCoreWSClient:
    """
    飞书插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 IncomingMessage（来自飞书）转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为飞书格式并发送
    - 处理 tool_call 事件（委托给 FeishuClient 执行）

    不负责：Session 管理、AI 推理
    """

    def __init__(
        self,
        core_url: str,           # e.g. "ws://127.0.0.1:8765"
        feishu_client: Any,      # FeishuClient 实例（用于发送消息）
        bot_id: str,
        project_path: str,
        on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None,
        data_dir: str = "",
        groups: dict | None = None,        # group_id -> GroupConfigEntry dict
        allowed_users: list | None = None, # P2P whitelist
    ):
        self.core_url = core_url
        self.feishu = feishu_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._on_message = on_message
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, str] = {}  # req_id → incoming message_id
        self._id_counter = 0
        # Stream accumulators keyed by incoming message_id (for buffering streaming chunks)
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        # 群聊历史：chat_id → 最近10条消息（内存滚动存储）
        self._group_history: dict[str, list[IncomingMessage]] = {}
        self._MAX_GROUP_HISTORY = 10
        self._data_dir = data_dir

        # Feishu 格式化管线
        self.formatter = ReplyFormatter()

        # Memory Manager（MCP 工具执行器）
        try:
            from supercc.claude.memory_manager import get_memory_manager
            self._memory_manager = get_memory_manager()
        except Exception:
            self._memory_manager = None

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info(f"[FeishuCore] Connected to core at {self.core_url}")

        # 启动读取循环
        asyncio.create_task(self._read_loop())

    async def _reconnect(self):
        """断开旧连接，重新连接核心 WebSocket。"""
        import websockets
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[FeishuCore] Reconnected to core")
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
                logger.warning("[FeishuCore] Connection closed, reconnecting...")
                break
            except Exception:
                logger.exception("[FeishuCore] Error reading message")

    async def _handle_core_message(self, data: dict):
        """处理核心发来的消息（Response 或 Event）。"""
        if "id" in data:
            # JSON-RPC Response：唤醒 Future，同时清理 typing mapping
            req_id = str(data.get("id"))
            msg_id = self._pending_message_ids.pop(req_id, None)
            if msg_id:
                try:
                    await self.feishu.add_typing_reaction(msg_id, emoji_type="DONE")
                except Exception:
                    pass
                # Flush and clean up the stream accumulator for this message
                if msg_id in self._accumulator_by_msg_id:
                    acc = self._accumulator_by_msg_id.pop(msg_id)
                    await acc.flush()
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                if data.get("error"):
                    fut.set_result(data)
                else:
                    fut.set_result(data.get("result"))
            return

        # Event notification
        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE:
            msg_id = params.get("message_id", "")
            extra = params.get("extra", {})
            mention_tag = extra.get("mention_tag", "")  # executor 已计算，直接使用
            content = params.get("content", "")

            if mention_tag:
                if msg_id and msg_id in self._accumulator_by_msg_id:
                    # 流式模式：在 flush 前追加 mention_tag 到 buffer
                    acc = self._accumulator_by_msg_id[msg_id]
                    async with acc._lock:
                        buffered = acc._buffer
                    if buffered and f'<at user_id=' not in buffered:
                        async with acc._lock:
                            acc._buffer += mention_tag
                elif content and f'<at user_id=' not in content:
                    # 非流式模式：在 content 末尾追加 mention_tag
                    params["content"] = content + mention_tag

            await self._render_and_send(params)
        elif method == Event.STREAM_CHUNK:
            # 流式输出中 - use accumulator for buffering
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == Event.PONG:
            pass  # 心跳响应

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为飞书格式并发送。

        RESPONSE 事件：
        - 有 accumulator（流式模式）：flush 后丢弃 RESPONSE 内容（内容已在 chunks 中）
        - 无 accumulator（非流式模式）：格式化后 safe send

        STREAM_CHUNK 事件：
        - 走缓冲区累积，由 accumulator 在 idle 超时或 tool call 时 flush
        """
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        card = params.get("extra", {}).get("card")

        if not content and not card:
            return

        if card:
            # Command result with card - flush any pending text, then send as interactive card
            if message_id and message_id in self._accumulator_by_msg_id:
                await self._accumulator_by_msg_id[message_id].flush()
            await self.feishu.send_interactive_card(chat_id, content)
            return

        if message_id and message_id in self._accumulator_by_msg_id:
            # 有 accumulator → 流式 chunks 或 RESPONSE flush 信号
            acc = self._accumulator_by_msg_id[message_id]
            if params.get("event") == Event.RESPONSE:
                # RESPONSE = 流式结束信号：flush 并清理 accumulator
                await acc.flush()
                del self._accumulator_by_msg_id[message_id]
            else:
                # STREAM_CHUNK：追加到缓冲区（accumulator 内部会定时 flush）
                await acc.add_text(content)
        elif message_id:
            # 首次收到该 message_id 的 chunk，创建 accumulator
            self._accumulator_by_msg_id[message_id] = StreamAccumulator(
                chat_id=chat_id,
                message_id=message_id,
                send_fn=lambda cid, mid, text: self._do_send_text(cid, text, mid),
            )
            await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            # 非流式完整响应：格式化后 safe send
            formatted = self.formatter.format_text(content)
            await self._safe_send(chat_id, message_id, formatted)

    async def _safe_send(self, chat_id: str, reply_to_message_id: str, text: str):
        """发送格式化文本，依次尝试：card → post → text，三级降级。

        Uses Interactive Card for content with fenced code blocks or tables,
        falls back to rich text post for plain markdown.
        Falls back to plain text if card/post sending fails.
        """
        if not text or not text.strip():
            return
        try:
            if should_use_card(text):
                try:
                    await self.feishu.send_interactive_reply(chat_id, text, reply_to_message_id)
                except Exception as card_error:
                    # 卡片失败，降级到 post
                    logger.warning(f"Card failed ({card_error}), falling back to post")
                    await self.feishu.send_post_reply(chat_id, text, reply_to_message_id)
            else:
                await self.feishu.send_post_reply(chat_id, text, reply_to_message_id)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")

    async def _do_send_text(self, chat_id: str, text: str, message_id: str) -> None:
        """Send text to Feishu (called by StreamAccumulator after buffering).

        Uses safe send: card → post → text fallback.
        """
        await self._safe_send(chat_id, message_id, text)

    async def _handle_tool_call(self, params: dict):
        """处理核心发来的工具调用请求。

        使用 ReplyFormatter 格式化工具结果，支持：
        - _DiffMarker → Edit Diff 彩色卡片
        - _MemoryCardMarker → 记忆工具卡片
        - _AskUserQuestionMarker → 问卷卡片
        - 其他 → backtick 格式 safe send
        """
        tool_name = params.get("tool_name", "")
        tool_input_raw = params.get("tool_input", {})
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # Flush any pending streaming text for this message before handling tool call
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        # 格式化工具结果
        tool_input_str = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")

        # 构建 format_tool_call kwargs
        kwargs: dict[str, Any] = {}
        if tool_name.startswith("mcp__SuperCC__Memory") and self._memory_manager:
            kwargs["memory_manager"] = self._memory_manager
            kwargs["default_project_path"] = self.project_path
            kwargs["platform"] = "feishu"
            kwargs["chat_id"] = chat_id

        result = self.formatter.format_tool_call(tool_name, tool_input_str, **kwargs)

        # 根据 result 类型渲染
        if isinstance(result, _DiffMarker):
            # Edit/Write → 彩色 diff 卡片
            cards = result.card if isinstance(result.card, list) else [result.card]
            for card in cards:
                try:
                    await self.feishu.send_edit_diff_card(chat_id, card, msg_id, log_reply=False)
                except Exception:
                    # 降级为带图标的纯文本
                    try:
                        data = json.loads(result.tool_input)
                        file_path = data.get("file_path", "unknown")
                        if result.tool_name == "Edit":
                            icon = "✏️"
                        elif result.tool_name.startswith("cc-"):
                            icon = "🧰"
                        elif result.tool_name == "Bash":
                            cmd = data.get("command", "")
                            if "~/.claude/skills/" in cmd or cmd.startswith("cc-"):
                                icon = "🧰"
                            else:
                                icon = "📝"
                        else:
                            icon = "📝"
                        fallback = f"{icon} **{result.tool_name}** — `{file_path}`"
                    except Exception:
                        fallback = f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`"
                    await self._safe_send(chat_id, msg_id, fallback)

        elif isinstance(result, list):
            # list[_DiffMarker]
            for marker in result:
                if isinstance(marker, _DiffMarker):
                    cards = marker.card if isinstance(marker.card, list) else [marker.card]
                    for card in cards:
                        try:
                            await self.feishu.send_edit_diff_card(chat_id, card, msg_id, log_reply=False)
                        except Exception:
                            try:
                                data = json.loads(marker.tool_input)
                                file_path = data.get("file_path", "unknown")
                                if marker.tool_name == "Edit":
                                    icon = "✏️"
                                elif marker.tool_name.startswith("cc-"):
                                    icon = "🧰"
                                elif marker.tool_name == "Bash":
                                    cmd = data.get("command", "")
                                    if "~/.claude/skills/" in cmd or cmd.startswith("cc-"):
                                        icon = "🧰"
                                    else:
                                        icon = "📝"
                                else:
                                    icon = "📝"
                                fallback = f"{icon} **{marker.tool_name}** — `{file_path}`"
                            except Exception:
                                fallback = f"🤖 **{marker.tool_name}**\n`{marker.tool_input[:500]}`"
                            await self._safe_send(chat_id, msg_id, fallback)

        elif isinstance(result, _MemoryCardMarker):
            # 记忆工具 → CardKit 格式，reply 到原始消息
            card = self._render_memory_card(result)
            try:
                await self.feishu.send_interactive(chat_id, card, msg_id)
            except Exception:
                await self._safe_send(chat_id, msg_id, str(card))

        elif isinstance(result, str) and tool_name.startswith("mcp__SuperCC__AskUserQuestion"):
            # AskUserQuestion → 尝试渲染为问卷卡片
            from supercc.adapter.feishu.format.questionnaire_card import parse_ask_user_question
            qdata = parse_ask_user_question(tool_input_str)
            if qdata is not None:
                # 包装为 _AskUserQuestionMarker 以复用 format_questionnaire_card
                from supercc.adapter.feishu.format.questionnaire_card import _AskUserQuestionMarker
                marker = _AskUserQuestionMarker(tool_name, tool_input_str)
                marker.data = qdata
                card = format_questionnaire_card(marker)
                try:
                    await self.feishu.send_edit_diff_card(chat_id, card, msg_id, log_reply=False)
                except Exception:
                    await self._safe_send(chat_id, msg_id, result)
            else:
                await self._safe_send(chat_id, msg_id, result)

        else:
            # 其他工具 → backtick 格式
            if isinstance(result, str):
                await self._safe_send(chat_id, msg_id, result)
            else:
                await self._safe_send(chat_id, msg_id, f"🤖 **{tool_name}**")

        # 发送 tool_result 回核心
        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": f"[{tool_name}] executed",
            "chat_id": chat_id,
        })

    def _render_memory_card(self, marker: _MemoryCardMarker) -> dict:
        """将 _MemoryCardMarker 渲染为 CardKit 原生格式。"""
        try:
            args = json.loads(marker.tool_input) if marker.tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = marker.tool_name.replace("mcp__SuperCC__", "")
        scope = "proj" if "Proj" in short else "user"
        card_type = marker.card_type or ""

        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

        header = f"🧠 **{short}**"
        if card_type == "search":
            q = args.get("query", "")
            header += f"  查询: 「{q}」"
        if scope == "proj":
            pp = args.get("project_path", "") or self.project_path
            if pp:
                header += f"  项目: {pp.split('/')[-1] or pp}"
        elif args.get("user_open_id"):
            header += f"  用户: {args['user_open_id']}"

        elements = []

        if card_type in ("add", "update"):
            if not marker.entries:
                elements.append({"tag": "markdown", "content": f"{header}\n\n_无结果_"})
            else:
                table_lines = "| 标题 | 内容摘要 | 关键词 |\n|------|----------|--------|\n"
                for e in marker.entries:
                    title = _esc(e.get("title", "")[:60])
                    content = _esc(e.get("content", "")[:50])
                    keywords = _esc(e.get("keywords", ""))
                    table_lines += f"| {title} | {content} | {keywords} |\n"
                elements.append({"tag": "markdown", "content": f"{header}\n\n{table_lines}"})

        elif card_type in ("list", "search"):
            total = len(marker.entries)
            header += f"（共 {total} 条）"
            if not marker.entries:
                elements.append({"tag": "markdown", "content": f"{header}\n\n_无结果_"})
            else:
                table_lines = "| # | 标题 | 内容摘要 | 关键词 | ID |\n|---|------|----------|--------|---|\n"
                for i, e in enumerate(marker.entries, 1):
                    title = _esc(e.get("title", "")[:40])
                    content = _esc(e.get("content", "")[:50])
                    keywords = _esc(e.get("keywords", ""))
                    mid = f"`{e.get('id', '')}`"
                    table_lines += f"| {i} | {title} | {content} | {keywords} | {mid} |\n"
                elements.append({"tag": "markdown", "content": f"{header}\n\n{table_lines}"})

        elif card_type == "delete":
            deleted_id = marker.entries[0].get("id", "") if marker.entries else ""
            elements.append({"tag": "markdown", "content": f"{header}\n\n| ID |\n|------|\n| `{deleted_id}` |\n"})

        else:
            table_lines = "| 参数 | 值 |\n|------|----|\n"
            for k, v in args.items():
                v_str = _esc(str(v))
                if len(v_str) > 80:
                    v_str = v_str[:80] + "…"
                table_lines += f"| `{k}` | {v_str} |\n"
            elements.append({"tag": "markdown", "content": f"{header}\n\n{table_lines}"})

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": elements},
        }

    async def _check_group_permissions(self, inbound, incoming: IncomingMessage) -> bool:
        """检查群聊权限。返回 True=允许通过，False=已拦截（已发送授权卡片）。

        权限规则：
        - P2P：检查 allowed_users 白名单
        - 群聊：检查 groups 配置（enabled / require_mention / allow_from）
        """
        key = inbound.session_key
        is_group = inbound.extra.get("is_group_chat", False)

        if is_group:
            entry = self._groups.get(key.chat_id)
            if entry is None:
                # 未知群：默认拒绝
                reason = "该群未配置使用权限，请联系管理员。"
                try:
                    card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "⛔ 无访问权限"},
                        },
                        "body": {
                            "elements": [
                                {"tag": "markdown", "content": reason},
                            ]
                        },
                    }
                    await self.feishu.send_card(key.chat_id, card)
                except Exception:
                    pass
                return False

            if not getattr(entry, "enabled", True):
                reason = "该群已被禁用。"
                try:
                    card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "⛔ 群聊已禁用"},
                        },
                        "body": {
                            "elements": [
                                {"tag": "markdown", "content": reason},
                            ]
                        },
                    }
                    await self.feishu.send_card(key.chat_id, card)
                except Exception:
                    pass
                return False

            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                # 群聊但没有 @CC
                reason = "请 @CC 我来使用 SuperCC。"
                try:
                    card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "需要 @CC"},
                        },
                        "body": {
                            "elements": [
                                {"tag": "markdown", "content": reason},
                            ]
                        },
                    }
                    await self.feishu.send_card(key.chat_id, card)
                except Exception:
                    pass
                return False

            allow_from = getattr(entry, "allow_from", [])
            if allow_from and inbound.user_open_id not in allow_from:
                reason = "你在该群中没有使用权限。"
                try:
                    card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "⛔ 无访问权限"},
                        },
                        "body": {
                            "elements": [
                                {"tag": "markdown", "content": reason},
                            ]
                        },
                    }
                    await self.feishu.send_card(key.chat_id, card)
                except Exception:
                    pass
                return False

            return True
        else:
            # P2P 白名单
            if self._allowed_users and inbound.user_open_id not in self._allowed_users:
                reason = "你不在允许使用列表中。"
                try:
                    card = {
                        "schema": "2.0",
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {"tag": "plain_text", "content": "⛔ 无访问权限"},
                        },
                        "body": {
                            "elements": [
                                {"tag": "markdown", "content": reason},
                            ]
                        },
                    }
                    await self.feishu.send_card(incoming.chat_id, card)
                except Exception:
                    pass
                return False
            return True

    async def _do_send(self, req: JsonRpcRequest, incoming: IncomingMessage) -> dict:
        """实际执行 WS 发送和响应等待。"""
        import websockets

        future = asyncio.Future()
        req_id = str(req.id)
        if not req_id or req_id == "None":
            logger.warning(f"[FeishuCore] invalid req_id: {req_id!r}, skipping")
            return {}
        self._pending_responses[req_id] = future
        self._pending_message_ids[req_id] = incoming.message_id

        try:
            await self._ws.send(json.dumps(req.to_dict()))
        except websockets.exceptions.ConnectionClosedError:
            # 断了就重连并重试一次
            logger.warning("[FeishuCore] send failed, reconnecting...")
            await self._reconnect()
            self._pending_responses[req_id] = future
            self._pending_message_ids[req_id] = incoming.message_id
            await self._ws.send(json.dumps(req.to_dict()))

        if future is None:
            logger.warning("[FeishuCore] future is None, skipping await")
            return {}

        try:
            result = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            logger.warning("[FeishuCore] response timeout")
            result = {}
        except TypeError as e:
            logger.error(f"[FeishuCore] await failed (future=None?): {e}")
            result = {}
        return result or {}
        return result or {}

    async def send_message(self, incoming: IncomingMessage) -> dict:
        """
        将 IncomingMessage 转发给核心，并等待响应。

        用于消息处理的主流程。
        最多重试 2 次（第一次失败后重连，再试一次）。
        """
        import websockets

        # 图片/文件消息：下载媒体，转换为本地路径 markdown
        if incoming.message_type in ("image", "file"):
            resolved = await self._resolve_media_markdown(incoming)
            if resolved:
                incoming = dataclass_replace(incoming, content=resolved)

        inbound = incoming_to_inbound(
            incoming,
            bot_id=self.bot_id,
            project_path=self.project_path,
        )

        # 群聊权限校验
        if not await self._check_group_permissions(inbound, incoming):
            return {}

        # ── 群聊非@mention消息：只存内存，通知core更新session ─────────────────
        if inbound.extra.get("is_group_chat") and not inbound.extra.get("mention_bot"):
            hist = self._group_history.setdefault(inbound.session_key.chat_id, [])
            hist.append(incoming)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist[:] = hist[-self._MAX_GROUP_HISTORY:]
            try:
                notify_req = JsonRpcRequest(
                    id=self._next_id(),
                    method="feishu.notify",
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
            logger.info(f"[FeishuCore] group msg stored, hist_len={len(hist)}")
            return {}

        # ── 群聊上下文 enrichment（历史、成员列表、引用消息）───────────────
        await self._enrich_group_context(inbound, incoming)

        # 添加 typing indicator: OK reaction 表示 AI 开始处理
        try:
            await self.feishu.add_typing_reaction(incoming.message_id, emoji_type="OK")
        except Exception:
            pass  # 失败不影响主流程

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

        # 最多重试 2 次
        for attempt in range(2):
            try:
                return await self._do_send(req, incoming)
            except websockets.exceptions.ConnectionClosedError:
                if attempt == 0:
                    logger.warning("[FeishuCore] connection dead, reconnecting...")
                    try:
                        await self._reconnect()
                    except Exception:
                        logger.exception("[FeishuCore] reconnect failed")
                        raise
                else:
                    logger.error("[FeishuCore] send failed after reconnect")
                    raise
        return {}

    async def _resolve_media_markdown(self, msg: Any) -> str | None:
        """解析消息中的媒体（图片/文件）为 markdown 格式。

        下载媒体到本地，返回 ![image](path) 或 [File: path] 格式。
        返回 None 表示解析失败。
        """
        msg_type = getattr(msg, "message_type", "") or msg.get("message_type", "")
        msg_id = getattr(msg, "message_id", "") or msg.get("message_id", "")

        if msg_type not in ("image", "file"):
            return None

        try:
            # 通过 get_message API 获取可靠的 content（WS 事件 content 可能缺 image_key）
            msg_data = await self.feishu.get_message(msg_id)
            if not msg_data:
                return None
            content_str = msg_data.get("content", "{}")
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except Exception:
            return None

        data_dir = self._data_dir or ""

        if msg_type == "image":
            file_key = content.get("image_key", "") if isinstance(content, dict) else ""
            if not file_key:
                return None
            try:
                base_path = self._make_image_path(data_dir, msg_id, file_key)
                data = await self.feishu.download_media(msg_id, file_key, msg_type="image")
                save_path = base_path + ".png"
                with open(save_path, "wb") as f:
                    f.write(data)
                logger.info(f"[FeishuCore] saved image to {save_path}")
                return f"![image]({save_path})"
            except Exception as e:
                logger.warning(f"[FeishuCore] image download failed: {e}")
                return None

        elif msg_type == "file":
            file_key = content.get("file_key", "") if isinstance(content, dict) else ""
            orig_name = content.get("file_name", "file") if isinstance(content, dict) else "file"
            file_type = content.get("file_type", "bin") if isinstance(content, dict) else "bin"
            if not file_key:
                return None
            try:
                save_path = self._make_file_path(data_dir, msg_id, orig_name, file_type)
                data = await self.feishu.download_media(msg_id, file_key, msg_type="file")
                with open(save_path, "wb") as f:
                    f.write(data)
                logger.info(f"[FeishuCore] saved file to {save_path}")
                return f"[File: {save_path}] ({orig_name})"
            except Exception as e:
                logger.warning(f"[FeishuCore] file download failed: {e}")
                return None

        return None

    def _make_image_path(self, data_dir: str, msg_id: str, image_key: str) -> str:
        import hashlib, os
        key_hash = hashlib.md5(image_key.encode()).hexdigest()[:8]
        directory = os.path.join(data_dir, ".supercc", "media") if data_dir else "/tmp/supercc_media"
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{msg_id}_{key_hash}")

    def _make_file_path(self, data_dir: str, msg_id: str, orig_name: str, file_type: str) -> str:
        import os
        directory = os.path.join(data_dir, ".supercc", "media") if data_dir else "/tmp/supercc_media"
        os.makedirs(directory, exist_ok=True)
        safe_name = "".join(c for c in orig_name if c.isalnum() or c in "._-") or "file"
        return os.path.join(directory, f"{msg_id}_{safe_name}")

    async def _enrich_group_context(self, inbound, incoming):
        """为群聊消息收集并注入上下文：历史、成员列表、引用消息、@mention规则。

        历史从内存（_group_history）中取；成员列表调用API；引用消息调用API。
        """
        if not inbound.extra.get("is_group_chat"):
            return

        chat_id = inbound.session_key.chat_id
        extra = inbound.extra

        # 1) 群历史（从内存，媒体按需解析）
        # 内存中的消息（IncomingMessage 对象），图片/文件需下载到本地再注入
        hist = self._group_history.get(chat_id, [])
        if hist:
            history_lines = []
            for h_msg in hist:
                h_msg_id = getattr(h_msg, "message_id", "") or (h_msg.get("message_id") if isinstance(h_msg, dict) else "")
                h_user_open_id = getattr(h_msg, "user_open_id", "") or (h_msg.get("user_open_id") if isinstance(h_msg, dict) else "")
                h_content = getattr(h_msg, "content", "") or (h_msg.get("content") if isinstance(h_msg, dict) else "")
                h_msg_type = getattr(h_msg, "message_type", "text") or (h_msg.get("message_type") if isinstance(h_msg, dict) else "text")
                h_raw = getattr(h_msg, "raw_content", "") or (h_msg.get("raw_content") if isinstance(h_msg, dict) else "")

                # 非文本类型用 raw_content
                text = h_content if h_msg_type == "text" else h_raw

                # 图片/文件：下载到本地，注入 markdown 路径
                if h_msg_type in ("image", "file"):
                    try:
                        resolved = await self._resolve_media_markdown(h_msg)
                        if resolved:
                            text = resolved
                            # 更新 hist 中的 content 避免重复下载
                            if hasattr(h_msg, "content"):
                                h_msg.content = resolved
                            elif isinstance(h_msg, dict):
                                h_msg["content"] = resolved
                    except Exception as e:
                        logger.warning(f"[FeishuCore] resolve media failed for {h_msg_id}: {e}")
                        text = f"{h_content} (媒体下载失败)" if h_content else ""

                # 获取发送者姓名
                sender_name = h_user_open_id
                if h_user_open_id:
                    try:
                        sender_name = await self.feishu.get_user_name(h_user_open_id)
                    except Exception:
                        pass

                if text:
                    history_lines.append(f"{sender_name}: {text[:200]}")
            if history_lines:
                extra["group_history"] = history_lines

        # 2) 群成员列表 + @mention 规则生成
        try:
            members = await self.feishu.get_chat_members(chat_id)
            if members:
                member_lines = []
                sender_name = None
                for m in members[:50]:
                    if isinstance(m, dict):
                        member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                        name = m.get("name") or m.get("bot_name", "")
                    else:
                        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", "")
                        name = getattr(m, "name", None) or ""
                    if member_id and name:
                        member_lines.append(f"  {name}: <at user_id=\"{member_id}\">{name}</at>")
                        if member_id == inbound.user_open_id and not sender_name:
                            sender_name = name
                extra["group_members"] = member_lines

                # 生成 @mention 规则（含发送者姓名）
                sender_display = sender_name or inbound.user_open_id
                mention_rules = (
                    f"【群聊规则】必须在最终回复里艾特@{sender_display}以及相关人员。"
                    f"使用飞书 @ 格式如：<at user_id=\"open_id\">姓名</at>。不得遗漏。\n"
                    + "\n".join(member_lines)
                )
                extra["mention_rules"] = mention_rules
        except Exception as e:
            logger.warning(f"[FeishuCore] failed to fetch group members: {e}")

        # 3) 引用消息内容（parent_id → get_message）
        parent_id = getattr(incoming, "parent_id", "") or ""
        if parent_id:
            try:
                quoted_msg = await self.feishu.get_message(parent_id)
                if quoted_msg:
                    body = quoted_msg.get("body", {})
                    quoted_text = body.get("content", "") if isinstance(body, dict) else ""
                    if quoted_text:
                        extra["quoted_content"] = quoted_text[:500]
            except Exception as e:
                logger.warning(f"[FeishuCore] failed to fetch quoted message {parent_id}: {e}")

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

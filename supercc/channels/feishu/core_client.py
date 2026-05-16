"""飞书插件的 Thin Client：连接核心 WebSocket 服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any, Callable, Awaitable

from supercc.core.protocol import (
    JsonRpcRequest,
    Event,
)
from supercc.channels.feishu.client import IncomingMessage
from supercc.channels.feishu.core_protocol import incoming_to_inbound
from supercc.channels.feishu.format.reply_formatter import ReplyFormatter, should_use_card
from supercc.channels.feishu.format.edit_diff import _DiffMarker
from supercc.channels.common.format import MemoryCardMarker
from supercc.channels.feishu.format.questionnaire_card import _AskUserQuestionMarker
from supercc.channels.feishu.format.agent_card import FeishuAgentCardMarker, FeishuCodexMarker
from supercc.channels.feishu.media import make_image_path, make_file_path, save_bytes
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
        self._reconnect_lock = asyncio.Lock()
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, str] = {}  # req_id → incoming message_id
        self._id_counter = 0
        # Stream accumulators keyed by incoming message_id (for buffering streaming chunks)
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        # Tracks message_ids that received STREAM_CHUNK (distinguishes AI queries from commands)
        self._streamed_msg_ids: set[str] = set()
        # 当前 chat 上下文（用于 command_progress 进度卡片）
        self._last_chat_id: str = ""
        self._last_message_id: str = ""
        # 群聊历史：chat_id → 最近10条消息（内存滚动存储）
        self._group_history: dict[str, list[IncomingMessage]] = {}
        self._MAX_GROUP_HISTORY = 10
        self._data_dir = data_dir

        # Feishu 格式化管线
        self.formatter = ReplyFormatter()

        # Memory Manager（MCP 工具执行器）
        try:
            from supercc.core.claude.memory_manager import get_memory_manager
            self._memory_manager = get_memory_manager()
        except Exception:
            self._memory_manager = None

    async def _send_auth(self):
        """发送 auth 消息到核心，完成身份认证。"""
        from supercc.config import get_config
        cfg = get_config()
        if cfg.core.token:
            await self._ws.send(json.dumps({
                "type": "auth",
                "token": cfg.core.token,
                "platform": "feishu"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "feishu"
            }))

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info(f"Connected to core at {self.core_url}")

        await self._send_auth()

        # 启动读取循环 + 心跳
        asyncio.create_task(self._read_loop())
        asyncio.create_task(self._ping_loop())

    async def _reconnect(self, jitter: bool = True):
        """断开旧连接，重新连接核心 WebSocket。

        可安全地从 _read_loop 和 _do_send 同时调用（_reconnect_lock 保证互斥）。
        调用前 _running 应保持 True。

        jitter=True 时重连前加 0~3s 随机延迟（防多插件同时重连 core）。"""
        import websockets
        if jitter:
            await asyncio.sleep(random.uniform(0, 3))
        async with self._reconnect_lock:
            # Guard: 另一个 task 已经重连好了
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
            logger.info("Reconnected to core")
            await self._send_auth()

    async def _read_loop(self):
        """持续读取核心发来的消息。连接断开时自动重连（自愈循环）。"""
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
                    logger.warning("Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("Error reading message\n%s", traceback.format_exc())

    async def _ping_loop(self):
        """定期 ping core，检测连接是否健康。超时则自动重连。

        应用层 ping（通过 JSON-RPC core.ping），比 TCP ping 更能反映
        完整的发送-处理-响应链路是否正常。"""
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
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "feishu"}
                ))
                await asyncio.wait_for(future, timeout=60)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                logger.warning("ping timeout (60s), reconnecting...")
                self._pending_responses.pop(req_id, None)
                await self._reconnect(jitter=False)
            except Exception:
                self._pending_responses.pop(req_id, None)
            else:
                self._pending_responses.pop(req_id, None)

    async def _handle_core_message(self, data: dict):
        """处理核心发来的消息（Response 或 Event）。"""
        if "id" in data:
            # JSON-RPC Response：唤醒 Future，同时清理 typing mapping
            req_id = str(data.get("id"))
            msg_id = self._pending_message_ids.pop(req_id, None)
            if msg_id:
                try:
                    await self.feishu.add_typing_reaction(msg_id, emoji_type="DONE")
                    logger.info("[typing] [done] message_id=%s", msg_id)
                except Exception:
                    pass
                # Flush and clean up the stream accumulator for this message
                if msg_id in self._accumulator_by_msg_id:
                    acc = self._accumulator_by_msg_id.pop(msg_id)
                    await acc.flush()
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                if not fut.done():
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
            content = params.get("content", "")
            is_group = extra.get("is_group_chat", False)
            sender_id = extra.get("user_open_id", "")
            sender_name = extra.get("sender_name", "")

            # ── 群聊 mention 检查：若 AI 未 mention 提问者，追加飞书 XML ───────
            if is_group and sender_id and sender_name:
                mention_xml = f'<at user_id="{sender_id}">{sender_name}</at>'
                # 检查 AI 是否已自然 mention
                if f'<at user_id="{sender_id}"' not in content:
                    if msg_id and msg_id in self._accumulator_by_msg_id:
                        # 流式模式：在 flush 前追加到 buffer
                        acc = self._accumulator_by_msg_id[msg_id]
                        async with acc._lock:
                            buffered = acc._buffer
                        if buffered:
                            async with acc._lock:
                                acc._buffer += f"\n{mention_xml}"
                    else:
                        # 非流式模式：在 content 末尾追加
                        params["content"] = content + f"\n{mention_xml}"

            await self._render_and_send(params)

            # session 切换通知（在 AI 响应后追加提示）
            session_info = extra.get("session_info", "")
            if session_info:
                chat_id = params.get("chat_id", "")
                await self._safe_send(chat_id, msg_id, session_info)
        elif method == Event.STREAM_CHUNK:
            # 流式输出中 - use accumulator for buffering
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == "restart":
            # core 通过 push_fn 主动推送的 restart 确认消息
            chat_id = params.get("chat_id", "")
            msg_id = params.get("message_id", "")
            content = params.get("content", "正在重启...")
            if content:
                formatted = self.formatter.format_text(content)
                await self._safe_send(chat_id, msg_id, formatted)
        elif method == "command_progress":
            await self._handle_command_progress(params)

    async def _handle_command_progress(self, params: dict):
        """渲染 restart/update 步骤进度卡片，发到飞书。

        参考 restart_impl.run_restart 的 feishu 通知格式。
        """
        event = params.get("event", "")
        step = params.get("step", 0)
        total = params.get("total", 0)
        label = params.get("label", "")
        status = params.get("status", "")
        detail = params.get("detail", "")
        success = params.get("success", False)
        target_pid = params.get("target_pid")
        new_pid = params.get("new_pid")

        # 获取 chat_id（需要从当前连接的上下文获取，这里存一份 mapping）
        chat_id = self._last_chat_id or ""
        reply_to = self._last_message_id or ""

        if not chat_id:
            logger.warning("[command_progress] no chat_id, skipping")
            return

        bar = "▓" * step + "░" * (total - step)

        if event == "restart":
            title_prefix = "正在重启"
            title_done = "✅ 重启完成"
        elif event == "update":
            title_prefix = "正在更新"
            title_done = "✅ 更新完成"
        else:
            title_prefix = f"正在执行 {event}"
            title_done = "✅ 执行完成"

        if status == "final":
            if event == "restart":
                body = (
                    f"**新进程 PID**: `{new_pid}`\n\n"
                    f"🎉 SuperCC 已重启，可以在飞书中继续对话了。"
                )
            elif event == "update":
                body = (
                    f"🎉 SuperCC 已更新，可以在飞书中继续对话了。"
                )
            else:
                body = detail

            card = f"## {title_done}\n\n{body}"
        else:
            step_labels = {
                "restart": ["🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
                "update":  ["📋 检查更新", "📦 检查新版本", "✅ 下载完成", "🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
            }
            labels = step_labels.get(event, [])
            step_label = labels[step - 1] if step <= len(labels) else f"步骤 {step}"

            if event == "restart":
                body = f"**当前目录**: `{detail}`\n\n{bar} `{step}/{total}` {step_label}\n\n⏳ 即将重启，请稍候..."
            elif event == "update":
                body = f"**版本**: `{detail}`\n\n{bar} `{step}/{total}` {step_label}\n\n⏳ 正在更新，请稍候..."
            else:
                body = f"{bar} `{step}/{total}` {step_label}\n\n⏳ {title_prefix}，请稍候..."

            card = f"## {title_prefix}\n\n{body}"

        try:
            await self.feishu.send_interactive_reply(chat_id, card, reply_to)
        except Exception:
            logger.warning("[command_progress] send failed, falling back to text")
            try:
                await self.feishu.send_text(chat_id, card)
            except Exception:
                pass

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

        if not content:
            return

        # 非流式 Event（如 restart/update）不经过 accumulator，直接发送
        if message_id and params.get("event") in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            formatted = self.formatter.format_text(content)
            await self._safe_send(chat_id, message_id, formatted)
            return

        if message_id and message_id in self._accumulator_by_msg_id:
            # 有 accumulator → 流式 chunks 或 RESPONSE flush 信号
            self._streamed_msg_ids.add(message_id)
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
            self._streamed_msg_ids.add(message_id)
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
        - MemoryCardMarker → 记忆工具卡片
        - _AskUserQuestionMarker → 问卷卡片
        - 其他 → backtick 格式 safe send
        """
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", {})
        tool_call_id = params.get("tool_call_id", "") or extra.get("tool_call_id", "")
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

        elif isinstance(result, MemoryCardMarker):
            # 记忆工具 → CardKit 格式，reply 到原始消息
            card = self._render_memory_card(result)
            try:
                await self.feishu.send_interactive(chat_id, card, msg_id)
            except Exception:
                await self._safe_send(chat_id, msg_id, str(card))

        elif isinstance(result, _AskUserQuestionMarker):
            # AskUserQuestion → 渲染为问卷卡片
            card = result.render()
            try:
                await self.feishu.send_edit_diff_card(chat_id, card, msg_id, log_reply=False)
            except Exception:
                await self._safe_send(chat_id, msg_id, result.render())

        elif isinstance(result, FeishuAgentCardMarker):
            # Agent → 精美飞书卡片
            card = result.render()
            try:
                await self.feishu.send_interactive(chat_id, card, msg_id)
            except Exception:
                await self._safe_send(chat_id, msg_id, result.render())

        elif isinstance(result, FeishuCodexMarker):
            # Codex → 精美飞书卡片
            card = result.render()
            try:
                await self.feishu.send_interactive(chat_id, card, msg_id)
            except Exception:
                await self._safe_send(chat_id, msg_id, result.render())

        else:
            # 其他工具 → backtick 格式
            if isinstance(result, str):
                await self._safe_send(chat_id, msg_id, result)
            else:
                await self._safe_send(chat_id, msg_id, f"🤖 **{tool_name}**")


    def _render_memory_card(self, marker: MemoryCardMarker) -> dict:
        """将 MemoryCardMarker 渲染为 CardKit 原生格式。"""
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
            # 未知群（entry is None）：使用 GroupConfigEntry 的默认行为（enabled=True, require_mention=True, allow_from=[]）
            # 等同于旧架构：无须注册，拉进群就能用（但仍需要 @CC）

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

            # 群聊但没有 @CC：静默忽略，不发任何通知
            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                logger.info(f"[GROUP] skip (no mention) chat_id={key.chat_id}")
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
            # P2P 白名单 + pairing 系统
            user_id = inbound.user_open_id
            # 重新加载 config（pairing approve 后 config.json 已更新）
            from supercc.config import reload_config
            cfg = reload_config()
            channel_cfg = getattr(cfg.channels, "feishu", None)
            allowed_users = list(getattr(channel_cfg, "allowed_users", [])) if channel_cfg else []
            # 先检查静态白名单（空列表 = 不设限，所有人都走配对检查）
            do_pairing_check = not allowed_users or user_id not in allowed_users
            if do_pairing_check:
                    # 未授权用户，生成 pairing code 并发送
                    try:
                        from supercc.core.pairing import get_pairing_store
                        store = get_pairing_store()
                        code = store.generate_code("feishu", user_id, inbound.extra.get("user_name", ""))
                        if code:
                            reason = (
                                f"你不在允许使用列表中。\n\n"
                                f"请联系管理员执行以下命令以获得使用权：\n\n"
                                f"```\nsupercc pairing approve {code}\n```"
                            )
                        else:
                            reason = "你不在允许使用列表中。\n\n配对码已生成，请联系管理员执行 approve。"
                    except Exception:
                        reason = "你不在允许使用列表中。\n\n配对系统暂时不可用，请联系机器人所有者。"

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

        # 保存当前 chat 上下文，供 command_progress 使用
        self._last_chat_id = incoming.chat_id
        self._last_message_id = incoming.message_id

        future = asyncio.Future()
        req_id = str(req.id)
        if not req_id or req_id == "None":
            logger.warning(f"invalid req_id: {req_id!r}, skipping")
            return {}
        self._pending_responses[req_id] = future
        self._pending_message_ids[req_id] = incoming.message_id

        try:
            await self._ws.send(json.dumps(req.to_dict()))
        except websockets.exceptions.ConnectionClosedError:
            # 断了就重连并重试一次
            logger.warning("send failed, reconnecting...")
            await self._reconnect(jitter=False)
            self._pending_responses[req_id] = future
            self._pending_message_ids[req_id] = incoming.message_id
            await self._ws.send(json.dumps(req.to_dict()))

        if future is None:
            logger.warning("future is None, skipping await")
            return {}

        try:
            result = await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            logger.warning("response timeout")
            result = {}
        except TypeError as e:
            logger.error(f"await failed (future=None?): {e}")
            result = {}
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
            try:
                resolved = await self._resolve_media_markdown(incoming)
                if resolved:
                    incoming = dataclass_replace(incoming, content=resolved)
            except Exception:
                logger.error("_resolve_media_markdown failed\n%s", traceback.format_exc())

        # ── 群聊 @mention 前缀剥离 ─────────────────────────────────────────
        # 当机器人被 @mention 时，content 包含 "@_user_1 " 前缀，
        # 这会导致命令检测（^/）失败。剥离后再送入 core 处理。
        if incoming.is_group_chat and incoming.mention_bot and incoming.message_type == "text":
            content = incoming.content
            # 剥离形如 "@_user_1 " 的前缀（保留后续内容）
            import re as _re
            stripped = _re.sub(r"^@\S+\s+", "", content, count=1)
            # 剥离 <at user_id="...">...</at> 标签（富文本格式）
            stripped = _re.sub(r"<at[^>]*>.*?</at>", "", stripped, count=1)
            if stripped != content:
                if stripped.strip():
                    incoming = dataclass_replace(incoming, content=stripped)
                    logger.info(f"[MENTION_STRIP] '{content}' -> '{stripped}'")
                else:
                    logger.info(f"[MENTION_STRIP] skipped (empty after strip) '{content}'")

        # ── 群聊上下文 enrichment（在 incoming 上构建 system_prompt）──────────
        # 注意：_enrich_group_context 必须在 incoming_to_inbound 之前调用，
        # 因为它写入 incoming.system_prompt，转换时再传入 InboundMessage
        if incoming.is_group_chat:
            try:
                await self._enrich_group_context(incoming)
            except Exception:
                logger.error("_enrich_group_context failed\n%s", traceback.format_exc())

        inbound = incoming_to_inbound(
            incoming,
            bot_id=self.bot_id,
            project_path=self.project_path,
            system_prompt=incoming.system_prompt,
            group_members=getattr(incoming, "group_members", None),
            group_context=getattr(incoming, "group_context", ""),
        )

        # ── 群聊所有消息：记录到 _group_history ────────────────────────────
        # 无论是否 @mention，所有群聊消息都要记录到 _group_history，
        # 以便为后续 @mention 消息提供会话上下文。
        if inbound.extra.get("is_group_chat"):
            hist = self._group_history.setdefault(inbound.session_key.chat_id, [])
            hist.append(incoming)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist.pop(0)

        # 群聊权限校验
        if not await self._check_group_permissions(inbound, incoming):
            return {}

        # ── 构建完整 content ─────────────────────────────────────────
        # content 是纯用户消息，不拼入群聊历史。
        # 群聊上下文（[最近消息]等）由 core 的 _build_prompt 从 extra.group_history 注入，
        # 这样 core 才能正确检测斜杠命令（_is_command 要求 content 以 / 开头）。
        full_content = inbound.content

        # 添加 typing indicator: OK reaction 表示 AI 开始处理
        try:
            await self.feishu.add_typing_reaction(incoming.message_id, emoji_type="OK")
            logger.info("[typing] [ok] message_id=%s", incoming.message_id)
        except Exception:
            pass  # 失败不影响主流程

        req = JsonRpcRequest(
            id=self._next_id(),
            method="feishu.message",
            platform=inbound.session_key.platform,
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": inbound.user_open_id,
                "project_path": inbound.session_key.project_path,
                "content": full_content,
                "system_prompt": inbound.system_prompt,
                "group_context": inbound.group_context or "",
                "message_type": inbound.message_type.value,
                "is_group_chat": inbound.extra.get("is_group_chat", False),
                "mention_bot": inbound.extra.get("mention_bot", False),
                "mention_ids": inbound.extra.get("mention_ids", []),
                "group_name": inbound.extra.get("group_name", ""),
                "thread_id": inbound.thread_id or "",
                "extra": inbound.extra,
            },
        )
        logger.debug(f"[SEND_TO_CORE] content={full_content[:100]!r} mention_bot={inbound.extra.get('mention_bot', False)}")

        # 最多重试 2 次（ConnectionClosedError、TypeError 均重试）
        for attempt in range(2):
            try:
                result = await self._do_send(req, incoming)
                # 同步响应（如命令结果、/restart、/update）
                # 不走 Event.RESPONSE，直接在 JSON-RPC Response 中返回
                if result:
                    inner = result.get("result", result)  # JSON-RPC result 包装层
                    result_event = inner.get("event", "") if isinstance(inner, dict) else ""
                    result_content = inner.get("content", "") if isinstance(inner, dict) else ""
                    if result_event in ("restart", "update"):
                        # 确认消息告知用户已收到指令，核心正在处理
                        msg_id = result.get("message_id", incoming.message_id)
                        if msg_id in self._streamed_msg_ids:
                            logger.info("[command] /%s skip (streamed)", result_event)
                        else:
                            logger.info("[command] /%s forwarding confirmation", result_event)
                            await self._safe_send(
                                incoming.chat_id,
                                msg_id,
                                self.formatter.format_text(result_content or f"正在处理 {result_event}..."),
                            )
                    elif result_content:
                        # AI 流式响应（有 STREAM_CHUNK）已通过 accumulator flush 发送，不走此路
                        msg_id = result.get("message_id", incoming.message_id)
                        if msg_id in self._streamed_msg_ids:
                            logger.info("[send_msg] skip %s (streamed)", msg_id)
                            return result
                        logger.info("[send_msg] send %s event=%s content_len=%d",
                                     msg_id, result_event, len(result_content))
                        # 普通命令结果（/stop、/help、/status 等）：渲染并发送
                        await self._safe_send(
                            incoming.chat_id,
                            result.get("message_id", incoming.message_id),
                            self.formatter.format_text(result_content),
                        )
                return result
            except websockets.exceptions.ConnectionClosedError:
                if attempt == 0:
                    logger.warning("connection dead, reconnecting...")
                    try:
                        await self._reconnect(jitter=False)
                    except Exception:
                        logger.error("reconnect failed\n%s", traceback.format_exc())
                        raise
                else:
                    logger.error("send failed after reconnect")
                    raise
            except TypeError:
                if attempt == 0:
                    logger.warning("TypeError, reconnecting...")
                    try:
                        await self._reconnect(jitter=False)
                    except Exception:
                        logger.error("reconnect failed\n%s", traceback.format_exc())
                    continue  # 继续下一次尝试
                else:
                    logger.error("TypeError persists after reconnect")
                    raise
        return {}

    async def _resolve_media_markdown(self, msg: Any) -> str | None:
        """解析消息中的媒体（图片/文件）为 markdown 格式。

        支持 simple 格式（{"Image_key": "..."}）和 rich post 格式（多图+穿插文字）。
        基于 v0.2.13 _preprocess_media 实现。
        """
        msg_type = getattr(msg, "message_type", "") or msg.get("message_type", "")
        msg_id = getattr(msg, "message_id", "") or msg.get("message_id", "")

        if msg_type not in ("image", "file"):
            return None

        # 避免重复下载
        content_raw = getattr(msg, "content", "") or ""
        if "![image]" in content_raw or "[File:" in content_raw:
            logger.info(f"[media] message {msg_id} already processed, skipping")
            return content_raw

        try:
            msg_data = await self.feishu.get_message(msg_id)
            if not msg_data:
                return None
            content_str = msg_data.get("content", "{}")
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
        except Exception:
            return None

        data_dir = self._data_dir or ""

        # ── Rich post 解析 helpers（来自 v0.2.13 _preprocess_media）────────────
        def _iter_documents(post: dict) -> list:
            """返回 post 中的文档列表。支持扁平 {"content": [...]} 和 locale 包裹 {"zh_cn": {...}} 格式。"""
            if not isinstance(post, dict) or not post:
                return []
            if "content" in post:
                return [post]
            return [doc for doc in post.values() if isinstance(doc, dict)]

        async def _post_to_markdown(post: dict) -> str:
            """将飞书 post content 转为 markdown，图片/文件下载到本地。"""
            docs = _iter_documents(post)
            if not docs:
                return ""
            locale = docs[0]
            lines = []
            title = locale.get("title")
            if title:
                lines.append(f"# {title}")
            for block in locale.get("content") or []:
                chunks = []
                for el in block or []:
                    if not isinstance(el, dict):
                        continue
                    tag = el.get("tag")
                    if tag == "text":
                        t = el.get("text") or ""
                        styles = el.get("style") or []
                        if "bold" in styles:
                            t = f"**{t}**"
                        if "italic" in styles:
                            t = f"*{t}*"
                        if "code" in styles:
                            t = f"`{t}`"
                        if "strikethrough" in styles:
                            t = f"~~{t}~~"
                        chunks.append(t)
                    elif tag == "img":
                        image_key = el.get("image_key", "")
                        if image_key:
                            base_path = make_image_path(data_dir, msg_id, image_key)
                            save_path = base_path + ".png"
                            logger.info(f"[media] downloading image key={image_key} → {save_path}")
                            data = await self.feishu.download_media(msg_id, image_key, msg_type="image")
                            save_bytes(save_path, data)
                            chunks.append(f"![image]({save_path})")
                    elif tag == "media":
                        file_key = el.get("file_key", "")
                        if file_key:
                            orig_name = el.get("file_name", "file")
                            file_type = el.get("file_type", "bin")
                            save_path = make_file_path(data_dir, msg_id, orig_name, file_type)
                            logger.info(f"[media] downloading file key={file_key} name={orig_name!r} → {save_path}")
                            data = await self.feishu.download_media(msg_id, file_key, msg_type="file")
                            save_bytes(save_path, data)
                            chunks.append(f"[File: {save_path}] ({orig_name})")
                    elif tag == "a":
                        chunks.append(f"[{el.get('text') or ''}]({el.get('href') or ''})")
                    elif tag == "at":
                        chunks.append(f"@{el.get('user_name') or el.get('user_id') or ''}")
                    elif tag == "emotion":
                        chunks.append(f":{el.get('emoji_type') or ''}:")
                    elif tag == "code_block":
                        lang = (el.get("language") or "").lower()
                        text = el.get("text") or ""
                        chunks.append(f"```{lang}\n{text}\n```")
                    elif tag == "hr":
                        chunks.append("---")
                    elif tag == "md":
                        chunks.append(el.get("text") or "")
                line = "".join(chunks)
                if line:
                    lines.append(line)
            return "\n\n".join(lines).strip()

        # ── 分发处理 ────────────────────────────────────────────────────────
        if msg_type == "image":
            if "image_key" in content:
                # Simple 格式：只有一张图片
                file_key = content.get("image_key", "")
                base_path = make_image_path(data_dir, msg_id, file_key)
                save_path = base_path + ".png"
                logger.info(f"[media] downloading image key={file_key} → {save_path}")
                data = await self.feishu.download_media(msg_id, file_key, msg_type="image")
                save_bytes(save_path, data)
                return f"![image]({save_path})"
            elif "content" in content:
                # Rich post 格式：多图+穿插文字
                result = await _post_to_markdown(content)
                return result if result else None
            else:
                logger.warning(f"[media] unknown image content structure: {content_str[:200]!r}")
                return None

        elif msg_type == "file":
            if "file_key" in content:
                file_key = content.get("file_key", "")
                orig_name = content.get("file_name", "file")
                file_type = content.get("file_type", "bin")
                save_path = make_file_path(data_dir, msg_id, orig_name, file_type)
                logger.info(f"[media] downloading file key={file_key} name={orig_name!r} → {save_path}")
                data = await self.feishu.download_media(msg_id, file_key, msg_type="file")
                save_bytes(save_path, data)
                return f"[File: {save_path}] ({orig_name})"
            elif "content" in content:
                result = await _post_to_markdown(content)
                return result if result else None
            else:
                return None

        return None

    async def _enrich_group_context(self, incoming):
        """为群聊消息收集并注入上下文：历史、成员列表、引用消息、@mention规则。

        历史从内存（_group_history）中取；成员列表调用API；引用消息调用API。
        system_prompt 写入 incoming.system_prompt（追加到 core system prompt 末尾）。
        """
        if not incoming.is_group_chat:
            return

        chat_id = incoming.chat_id

        # ── 首次 @mention 时检查飞书权限，缺失则发授权卡片 ────────────────
        # 权限不足时只通知，不阻塞后续处理
        perm_key = f"_perm_checked_{chat_id}"
        if not getattr(self, perm_key, False):
            setattr(self, perm_key, True)
            if incoming.mention_bot:
                try:
                    perm = await self.feishu.check_group_permissions(chat_id)
                    auth_url = perm.get("auth_url", "")
                    missing = []
                    if not perm.get("history_ok"):
                        missing.append("读取群聊历史（im:message）")
                    if not perm.get("members_ok"):
                        missing.append("读取群成员信息（im:chat.member:read）")
                    if missing:
                        card = {
                            "schema": "2.0",
                            "config": {"wide_screen_mode": True},
                            "body": {
                                "elements": [
                                    {"tag": "markdown", "content": "## ⚠️ 权限不足，无法正常服务\n\n当前机器人缺少以下权限：\n\n" + "\n".join(f"- {m}" for m in missing) + "\n\n请管理员点击下方按钮前往授权。"},
                                    {"tag": "action", "actions": [
                                        {"tag": "button", "text": "前往授权", "url": auth_url}
                                    ]},
                                ]
                            }
                        }
                        try:
                            await self.feishu.send_card(chat_id, card)
                        except Exception as card_err:
                            # 卡片发送失败时降级为 markdown
                            fallback = (
                                "⚠️ **权限不足，无法正常服务**\n\n"
                                + "\n".join(f"- {m}" for m in missing)
                                + "\n\n请管理员点击 [前往飞书开放平台授权]("
                                + auth_url
                                + ")"
                            )
                            try:
                                await self.feishu.send_post(chat_id, fallback)
                            except Exception:
                                pass
                            logger.warning(f"[GROUP_PERM] card send failed, fallback text sent: {card_err}")
                except Exception as ex:
                    logger.warning(f"[GROUP_PERM] permission check failed: {ex}")

        # 1) 先拉成员列表，建立 open_id → name 映射（复用，节省 API 调用）
        name_by_id: dict[str, str] = {}
        try:
            members = await self.feishu.get_chat_members(chat_id)
            for m in members:
                if isinstance(m, dict):
                    member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                    name = m.get("name") or m.get("bot_name", "")
                else:
                    member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", "")
                    name = getattr(m, "name", None) or ""
                if member_id and name:
                    name_by_id[member_id] = name
        except Exception as e:
            members = None
            logger.warning(f"[GROUP] get_chat_members failed: {e}")
        # 归一化为 plain dict，避免 lark SDK ListMember 对象无法 JSON 序列化
        incoming.group_members = []
        for m in (members or []):
            if isinstance(m, dict):
                incoming.group_members.append({
                    "member_id": m.get("member_id", ""),
                    "open_id": m.get("open_id", ""),
                    "name": m.get("name", ""),
                    "bot_id": m.get("bot_id", ""),
                    "bot_name": m.get("bot_name", ""),
                })
            else:
                incoming.group_members.append({
                    "member_id": getattr(m, "member_id", ""),
                    "open_id": getattr(m, "open_id", ""),
                    "name": getattr(m, "name", ""),
                    "bot_id": getattr(m, "bot_id", ""),
                    "bot_name": getattr(m, "bot_name", ""),
                })

        # 2) 群历史（从内存，媒体按需解析）
        # 用 name_by_id 解析发送者姓名，不再单独调 get_user_name API
        hist = self._group_history.get(chat_id, [])
        history_context = None
        if hist:
            history_lines = []
            for h_msg in hist:
                h_msg_id = getattr(h_msg, "message_id", "") or (h_msg.get("message_id") if isinstance(h_msg, dict) else "")
                # 跳过当前消息，避免重复注入
                if h_msg_id == incoming.message_id:
                    continue
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
                        logger.warning(f"[GROUP] resolve media failed for {h_msg_id}: {e}")
                        text = f"{h_content} (媒体下载失败)" if h_content else ""

                # 发送者姓名优先从成员列表查，兜底用 open_id
                sender_name = name_by_id.get(h_user_open_id) or h_user_open_id

                if text:
                    history_lines.append(f"{sender_name}: {text[:200]}")
            if history_lines:
                history_context = "[最近消息]\n  " + "\n  ".join(history_lines)
            else:
                history_context = None

        system_parts = []

        # 群名称（从 API 查一次，FeishuClient 内部缓存）
        group_name = ""
        try:
            group_name = await self.feishu.get_chat_name(chat_id)
        except Exception:
            pass
        if group_name:
            system_parts.append(f"[群聊: {group_name}]")

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
                    if member_id == incoming.user_open_id and not sender_name:
                        sender_name = name
            if member_lines:
                system_parts.append("\n".join(member_lines))
                sender_display = sender_name or incoming.user_open_id
                system_parts.append(
                    f"【群聊规则】必须在最终回复里艾特@{sender_display}以及相关人员。"
                    f"使用飞书 @ 格式如：<at user_id=\"open_id\">姓名</at>。不得遗漏。"
                )

        # 3) 引用消息内容（parent_id → get_message）- 进 group_context
        context_parts = []
        parent_id = getattr(incoming, "parent_id", "") or ""
        if parent_id:
            try:
                quoted_msg = await self.feishu.get_message(parent_id)
                if quoted_msg:
                    body = quoted_msg.get("body", {})
                    quoted_text = body.get("content", "") if isinstance(body, dict) else ""
                    if quoted_text:
                        context_parts.append(f"[引用消息] {quoted_text[:500]}")
            except Exception as e:
                logger.warning(f"failed to fetch quoted message {parent_id}: {e}")

        # 4) 群聊历史（[最近消息]）- 进 group_context
        if history_context:
            context_parts.append(history_context)

        if system_parts:
            incoming.system_prompt = "\n".join(system_parts)
        if context_parts:
            incoming.group_context = "\n".join(context_parts)

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()

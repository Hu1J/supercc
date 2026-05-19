"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.channels.common.format import MemoryCardMarker
from supercc.channels.common.media import save_bytes
from supercc.channels.wecom.client import WeComClient
from supercc.channels.wecom.core_protocol import incoming_to_inbound

logger = logging.getLogger(__name__)


class WeComReplyFormatter:
    """Format tool call results for WeCom (limited card support)."""

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

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "wecom",
        chat_id: str = "",
        bot_id: str = "",
        user_open_id: str = "",
    ) -> MemoryCardMarker | None:
        """格式化记忆 MCP 工具调用为卡片标记（WeCom markdown 版本）。"""
        try:
            args = json.loads(tool_input) if tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = tool_name.replace("mcp__SuperCC__", "")
        scope = "proj" if "Proj" in short else "user"
        card_type = short.lower().replace("mcp__supercc__memory", "")
        if card_type == "add":
            card_type = "add"
        elif card_type == "update":
            card_type = "update"
        elif card_type == "delete":
            card_type = "delete"
        elif card_type == "list":
            card_type = "list"
        elif card_type == "search":
            card_type = "search"

        project_path = args.get("project_path", "") or default_project_path
        query = args.get("query", "")
        entries = []

        if memory_manager is not None:
            try:
                if scope == "proj":
                    if card_type == "list":
                        mems = memory_manager.get_project_memories(project_path, platform=platform, chat_id=chat_id)
                        entries = [{"id": m.id, "title": m.title,
                                    "content": m.content, "keywords": m.keywords} for m in mems]
                    elif card_type == "search" and query:
                        results = memory_manager.search_project_memories(query, project_path, platform=platform, chat_id=chat_id)
                        entries = [{"id": r.memory.id, "title": r.memory.title,
                                    "content": r.memory.content,
                                    "keywords": r.memory.keywords} for r in results]
                else:
                    # user scope
                    user_open_id = user_open_id or args.get("user_open_id", "")
                    bot_id = bot_id or args.get("bot_id", "")
                    if user_open_id:
                        if card_type == "list":
                            prefs = memory_manager.get_preferences_by_user(user_open_id, platform=platform, bot_id=bot_id)
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
                        elif card_type == "search" and query:
                            prefs = memory_manager.search_preferences(query, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
            except Exception:
                pass

        # add/update/delete 的 fallback：直接从入参构造条目
        if not entries and card_type in ("add", "update"):
            entries = [{
                "title": args.get("title", ""),
                "content": args.get("content", ""),
                "keywords": args.get("keywords", ""),
                "id": args.get("id", "") or "(新增)"}]
        elif not entries and card_type == "delete":
            entries = [{"id": args.get("id", "") or ""}]

        return MemoryCardMarker(tool_name, card_type, entries, tool_input)

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "wecom",
        chat_id: str = "",
        bot_id: str = "",
        user_open_id: str = "",
    ) -> str | MemoryCardMarker:
        """Format a tool call notification as markdown text or MemoryCardMarker."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit → diff markdown
        if tool_name == "Edit":
            from supercc.channels.wecom.format.edit_diff import format_edit_markdown
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                diff_lines = data.get("diff_lines", [])
                return format_edit_markdown(file_path, diff_lines)
            except (json.JSONDecodeError, KeyError):
                return f"{icon} **{short_name}**"

        # Write → diff markdown
        if tool_name == "Write":
            from supercc.channels.wecom.format.edit_diff import format_write_markdown
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                content = data.get("content", [])
                if isinstance(content, str):
                    content = content.splitlines()
                return format_write_markdown(file_path, content)
            except (json.JSONDecodeError, KeyError):
                return f"{icon} **{short_name}**"

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

        # AskUserQuestion → 问卷 markdown
        if tool_name == "AskUserQuestion":
            from supercc.channels.wecom.format.questionnaire_card import format_questionnaire_markdown
            return format_questionnaire_markdown(tool_input)

        # Agent → Agent markdown
        if tool_name == "Agent":
            from supercc.channels.wecom.format.agent_card import format_agent_markdown
            return format_agent_markdown(tool_input)

        # mcp__codex__codex → Codex markdown
        if tool_name == "mcp__codex__codex":
            from supercc.channels.wecom.format.agent_card import format_codex_markdown
            try:
                data = json.loads(tool_input) if tool_input else {}
            except json.JSONDecodeError:
                data = {}
            event_type = data.get("event_type", "text")
            content = data.get("content", tool_input or "")
            extra = {"model": data.get("model", "")} if data.get("model") else None
            return format_codex_markdown(event_type, content, extra)

        # Memory MCP tools → MemoryCardMarker（需查库）
        if tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            marker = self._format_memory_tool(
                tool_name, tool_input,
                memory_manager=memory_manager,
                default_project_path=default_project_path,
                platform=platform,
                chat_id=chat_id,
                bot_id=bot_id,
                user_open_id=user_open_id,
            )
            if marker is not None:
                return marker

        # Default: icon + name + first 100 chars of input
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg


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
        data_dir: str = "",
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.ws_client = ws_client       # SDK WSClient
        self.wecom = wecom_client        # WeComClient (消息发送)
        self.bot_id = bot_id
        self.project_path = project_path
        self._data_dir = data_dir
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        # req_id → (message_id, chat_id)
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0
        self._sent_message_ids: set[str] = set()       # 幂等性（主动发送去重）
        # 流式消息 ID 追踪（避免 RESPONSE 重复发送）
        self._streamed_msg_ids: set[str] = set()
        # WeComSendFile 调用标记：发文件后 WS 会被踢，下次发送前需重连
        self._wecom_sendfile_called: bool = False
        # Stream accumulators keyed by message_id (for buffering streaming chunks)
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        # 当前 chat 上下文（用于 command_progress 进度卡片）
        self._last_chat_id: str = ""
        self._last_message_id: str = ""
        # 群聊历史：chat_id → 最近10条消息（内存滚动存储）
        self._group_history: dict[str, list[dict]] = {}
        self._MAX_GROUP_HISTORY = 10
        # message_id → (user_open_id, bot_id) 映射，用于 _handle_tool_call 时传递用户身份
        self._msg_ctx: dict[str, tuple[str, str]] = {}
        # 媒体缓存：message_id → 本地保存路径（避免重复下载）
        self._media_cache: dict[str, str] = {}

        # WeCom 格式化管线
        self.formatter = WeComReplyFormatter()
        # 重连互斥锁（防止 _reconnect 和 _read_loop 并发调用）
        self._reconnect_lock = asyncio.Lock()
        # Memory Manager（MCP 工具执行器）
        try:
            from supercc.core.memory_manager import get_memory_manager
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
                "platform": "wecom"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "wecom"
            }))

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[WeComCore] Connected to core")
        await self._send_auth()
        asyncio.create_task(self._read_loop())
        asyncio.create_task(self._ping_loop())

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
                    logger.warning("[WeComCore] Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("[WeComCore] Error reading message\n%s", traceback.format_exc())

    async def _reconnect(self, jitter: bool = True):
        """断开旧连接，重新连接核心 WebSocket。

        可安全地从 _read_loop 和 _do_send 同时调用（_reconnect_lock 保证互斥）。
        jitter=True 时重连前加 0~3s 随机延迟（防多插件同时重连 core）。"""
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
            logger.info("[WeComCore] Reconnected to core")
            await self._send_auth()

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
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "wecom"}
                ))
                await asyncio.wait_for(future, timeout=60)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                logger.warning("[WeComCore] ping timeout (60s), reconnecting...")
                self._pending_responses.pop(req_id, None)
                await self._reconnect(jitter=False)
            except Exception:
                self._pending_responses.pop(req_id, None)
            else:
                self._pending_responses.pop(req_id, None)

    async def _handle_core_message(self, data: dict):
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored is not None:
                logger.debug(f"[WeComCore] response for req.id={req_id}, stored={stored}")
            if stored:
                msg_id, chat_id = stored
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
            extra = params.get("extra", {})
            content = params.get("content", "")
            is_group = extra.get("is_group_chat", False)
            sender_id = extra.get("user_open_id", "")
            session_info = extra.get("session_info", "")
            logger.debug(f"[WeComCore] RESPONSE event: message_id={str(params.get('message_id') or '')[:20]}, content_len={len(content) if content else 0}")

            # ── 群聊 mention：追加 @userid 纯文本 ──────────────────────────────
            if is_group and sender_id:
                # 检查 AI 是否已自然 mention
                if f'@{sender_id}' not in content:
                    params["content"] = content + f"@{sender_id}"

            await self._render_and_send(params)
            # session 切换通知（在 AI 响应后追加提示）
            if session_info:
                chat_id = params.get("chat_id", "")
                await self.wecom.send_text(chat_id, session_info)
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
                await self.wecom._ws.reconnect()
                await self.wecom.send_text(chat_id, content)
        elif method == Event.PONG:
            pass  # 心跳响应
        elif method == "command_progress":
            await self._handle_command_progress(params)
        elif method == "cron_progress":
            await self._handle_cron_progress(params)
        elif method == "cron_result":
            await self._handle_cron_result(params)
        elif method == Event.NOTIFICATION:
            # 主动通知（如上下文超限提示）
            chat_id = params.get("chat_id", "")
            content = params.get("content", "")
            if content:
                await self.wecom.send_text(chat_id, content)

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为企业微信格式并发送。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        # 非流式 Event（如 restart/update）不经过 accumulator，直接发送
        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            await self.wecom.send_text(chat_id, content)
            return

        # Buffer text chunks for efficient batched sending
        if message_id:
            if message_id in self._accumulator_by_msg_id:
                # 已有 accumulator
                self._streamed_msg_ids.add(message_id)
                acc = self._accumulator_by_msg_id[message_id]
                if event == Event.RESPONSE:
                    # RESPONSE = 流式结束信号：立即 flush 并清理 accumulator
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
            # No message_id (e.g. final RESPONSE without streaming) - send directly
            await self.wecom.send_markdown(chat_id, content)

    async def _do_send_text(self, chat_id: str, text: str, message_id: str) -> None:
        """Send text to WeCom via proactive send (no response_url timeout).

        统一使用 send_markdown，WeCom 支持 markdown 渲染。
        不使用 template card（不支持 markdown）。
        """
        logger.info(f"[WeComCore] _do_send_text: chat_id={chat_id}, message_id={message_id[:20] if message_id else 'None'}, text_len={len(text)}")

        # WeComSendFile 后重连 WS，确保用有效凭证连接
        if self._wecom_sendfile_called:
            self._wecom_sendfile_called = False
            logger.info("[WeComCore] Reconnecting WS after WeComSendFile...")
            await self.wecom._ws.reconnect()

        sent_ok = False
        try:
            ack = await self.wecom.send_markdown(chat_id, text)
            logger.debug(f"[WeComCore] send_markdown ack: errcode={ack.get('errcode')}, errmsg={ack.get('errmsg')}")
            if ack.get("errcode") == 0:
                sent_ok = True
            else:
                # markdown 失败，尝试纯文本 fallback
                ack2 = await self.wecom.send_text(chat_id, text[:2000])
                logger.debug(f"[WeComCore] send_text fallback ack: errcode={ack2.get('errcode')}, errmsg={ack2.get('errmsg')}")
                if ack2.get("errcode") == 0:
                    sent_ok = True
        except Exception as e:
            logger.warning(f"[WeComCore] send failed: {e}")

        if not sent_ok and message_id:
            # 流式发送失败时放行回调路径（让 callback 补发）
            self._streamed_msg_ids.discard(message_id)

    async def _handle_command_progress(self, params: dict):
        """渲染 restart/update 步骤进度卡片，发到企业微信。"""
        event = params.get("event", "")
        step = params.get("step", 0)
        total = params.get("total", 0)
        label = params.get("label", "")
        status = params.get("status", "")
        detail = params.get("detail", "")
        success = params.get("success", False)
        target_pid = params.get("target_pid")
        new_pid = params.get("new_pid")

        chat_id = self._last_chat_id or ""

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
                body = f"新进程 PID: {new_pid}\n\nSuperCC 已重启，可以在企业微信中继续对话了。"
            elif event == "update":
                body = "SuperCC 已更新，可以在企业微信中继续对话了。"
            else:
                body = detail
            text = f"{title_done}\n\n{body}"
        else:
            step_labels = {
                "restart": ["🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
                "update":  ["📋 检查更新", "📦 检查新版本", "✅ 下载完成", "🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
            }
            labels = step_labels.get(event, [])
            step_label = labels[step - 1] if step <= len(labels) else f"步骤 {step}"

            if event == "restart":
                body = f"当前目录: {detail}\n\n{bar} {step}/{total} {step_label}\n\n⏳ 即将重启，请稍候..."
            elif event == "update":
                body = f"版本: {detail}\n\n{bar} {step}/{total} {step_label}\n\n⏳ 正在更新，请稍候..."
            else:
                body = f"{bar} {step}/{total} {step_label}\n\n⏳ {title_prefix}，请稍候..."

            text = f"{title_prefix}\n\n{body}"

        try:
            await self.wecom.send_markdown(chat_id, text)
        except Exception:
            try:
                await self.wecom.send_text(chat_id, text[:2000])
            except Exception as e:
                logger.warning("[command_progress] send failed: %s", e)

    async def _handle_cron_progress(self, params: dict):
        """处理 cron 中间过程消息。"""
        job_id = params.get("job_id", "")
        content = params.get("content", "")
        chat_id = params.get("chat_id") or self._last_chat_id or ""

        if not chat_id:
            logger.warning("[cron_progress] no chat_id, skipping")
            return

        if not isinstance(content, str):
            content = str(content)

        try:
            await self.wecom.send_markdown(chat_id, content)
        except Exception:
            try:
                await self.wecom.send_text(chat_id, content[:2000])
            except Exception as e:
                logger.warning(f"[cron_progress] failed to send: {e}")

    async def _handle_cron_result(self, params: dict):
        """处理 cron 最终结果消息。"""
        job_name = params.get("job_name", "")
        content = params.get("content", "")
        error = params.get("error")
        chat_id = params.get("chat_id") or self._last_chat_id or ""

        if not chat_id:
            logger.warning("[cron_result] no chat_id, skipping")
            return

        if not isinstance(content, str):
            content = str(content)

        try:
            if error:
                text = f"⏰ **{job_name}**\n\n❌ 错误: {error}"
                await self.wecom.send_text(chat_id, text)
            else:
                try:
                    await self.wecom.send_markdown(chat_id, content)
                except Exception:
                    await self.wecom.send_text(chat_id, content[:2000])
        except Exception as e:
            logger.warning(f"[cron_result] failed to send: {e}")

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：格式化工具结果并发送给用户。"""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        tool_call_id = params.get("tool_call_id", "") or extra.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # 检测 WeComSendFile 调用：它会导致 channel 的 WS 被踢下线
        if tool_name and "WeComSendFile" in tool_name:
            logger.info("[WeComCore] WeComSendFile detected, will reconnect before next send")
            self._wecom_sendfile_called = True

        # Flush any pending streaming text for this message before handling tool call
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        # 从 _msg_ctx 取出 user_open_id + bot_id
        user_open_id_from_ctx = ""
        bot_id_from_ctx = ""
        if msg_id and msg_id in self._msg_ctx:
            user_open_id_from_ctx, bot_id_from_ctx = self._msg_ctx.pop(msg_id)

        result = self.formatter.format_tool_call(
            tool_name, tool_input,
            memory_manager=self._memory_manager,
            default_project_path=self.project_path,
            platform="wecom",
            chat_id=chat_id,
            user_open_id=user_open_id_from_ctx,
            bot_id=bot_id_from_ctx,
        )

        # MemoryCardMarker → 渲染为 markdown 并发送
        if isinstance(result, MemoryCardMarker):
            text = result.render()
            try:
                await self.wecom.send_markdown(chat_id, text)
            except Exception:
                try:
                    await self.wecom.send_text(chat_id, text[:2000])
                except Exception:
                    pass
        else:
            # 格式化文本（Agent/Codex/Edit/Write/AskUserQuestion 等）→ 发送给用户
            text = str(result) if result else ""
            if text:
                try:
                    await self.wecom.send_markdown(chat_id, text)
                except Exception:
                    try:
                        await self.wecom.send_text(chat_id, text[:2000])
                    except Exception:
                        pass

    async def _check_group_permissions(self, inbound) -> bool:
        """检查群聊权限。返回 True=允许通过，False=已拦截（已发送授权卡片）。"""
        is_group = inbound.extra.get("is_group_chat", False)

        if is_group:
            entry = self._groups.get(inbound.session_key.chat_id)
            # 未知群（entry is None）：使用默认行为，等同于飞书——无须注册，拉进群就能用（但仍需要 @CC）

            if not getattr(entry, "enabled", True):
                reason = "该群已被禁用。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception as e:
                    logger.error(f"[WeComCore] group auth card failed (disabled): {e}")
                return False

            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                reason = "请 @CC 我来使用 SuperCC。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception as e:
                    logger.error(f"[WeComCore] group auth card failed (mention): {e}")
                return False

            allow_from = getattr(entry, "allow_from", [])
            if allow_from and inbound.user_open_id not in allow_from:
                reason = "你在该群中没有使用权限。"
                try:
                    await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason)
                except Exception as e:
                    logger.error(f"[WeComCore] group auth card failed (allow_from): {e}")
                return False

            return True
        else:
            # P2P 白名单 + pairing 系统
            user_id = inbound.user_open_id
            # 重新加载 config（pairing approve 后 config.json 已更新）
            from supercc.config import reload_config
            cfg = reload_config()
            channel_cfg = getattr(cfg.channels, "wecom", None)
            allowed_users = list(getattr(channel_cfg, "allowed_users", [])) if channel_cfg else []
            # 先检查静态白名单（空列表 = 不设限，所有人都走配对检查）
            do_pairing_check = not allowed_users or user_id not in allowed_users
            logger.debug(f"[WeComCore] P2P check: user_id={user_id}, allowed_users={allowed_users}, do_pairing_check={do_pairing_check}")
            if do_pairing_check:
                    # 未授权用户，生成 pairing code 并发送
                    try:
                        from supercc.core.pairing import get_pairing_store
                        store = get_pairing_store()
                        code = store.generate_code("wecom", user_id, inbound.extra.get("user_name", ""))
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
                        reply_req_id = self.ws_client.get_reply_req_id(inbound.message_id) or ""
                        logger.info(f"[WeComCore] sending authorization card to chat_id={inbound.session_key.chat_id}, reply_req_id={reply_req_id[:20] if reply_req_id else 'None'}")
                        await self.wecom.send_authorization_card(inbound.session_key.chat_id, reason, reply_req_id=reply_req_id)
                        logger.info(f"[WeComCore] authorization card sent successfully")
                    except Exception as e:
                        logger.error(f"[WeComCore] send_authorization_card failed: {e}", exc_info=True)
                    return False
            return True

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心，并等待响应。"""
        logger.debug(f"[WeComCore] ★ send_message called: msgtype={msg.get('msgtype')}, msgid={str(msg.get('msgid', ''))[:20]}, from={msg.get('from', {}).get('userid', '?')}")
        # 保存当前 chat 上下文，供 command_progress 使用
        # 图片/文件：解析 url+aeskey，下载到本地后转为 markdown 路径
        # 直接修改 msg 的 content，这样 incoming_to_inbound 会拿到已解析的内容
        msg_type = msg.get("msgtype", "text")
        if msg_type == "image":
            img = msg.get("image", {})
            url = img.get("url", "")
            aeskey = img.get("aeskey", "")
            msg_id = msg.get("msgid", "")
            sender = msg.get("from", {}).get("userid", "")
            resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "image", sender)
            if resolved:
                msg["_resolved_content"] = resolved
        elif msg_type == "mixed":
            # 解析混合消息：遍历所有 item，下载图片并组合文本
            mixed = msg.get("mixed", {})
            msg_items = mixed.get("msg_item", [])
            parts = []
            for item in msg_items:
                item_type = item.get("msgtype", "")
                if item_type == "text":
                    text_content = item.get("text", {}).get("content", "")
                    if text_content:
                        parts.append(text_content)
                elif item_type == "image":
                    img = item.get("image", {})
                    url = img.get("url", "")
                    aeskey = img.get("aeskey", "")
                    if url and aeskey:
                        sender = msg.get("from", {}).get("userid", "")
                        resolved = await self._download_and_resolve_media(msg.get("msgid", ""), url, aeskey, "image", sender)
                        if resolved:
                            parts.append(resolved)
                        else:
                            parts.append("[图片]")
                    else:
                        parts.append("[图片]")
                elif item_type == "file":
                    file_info = item.get("file", {})
                    fname = file_info.get("name", "文件")
                    parts.append(f"[文件: {fname}]")
            msg["_resolved_content"] = "\n".join(parts) if parts else "[混合消息]"
        elif msg_type == "file":
            file_info = msg.get("file", {})
            url = file_info.get("url", "")
            aeskey = file_info.get("aeskey", "")
            fname = file_info.get("name", "file")
            msg_id = msg.get("msgid", "")
            sender = msg.get("from", {}).get("userid", "")
            resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "file", sender, fname)
            if resolved:
                msg["_resolved_content"] = resolved

        # ── 处理 quote 引用附件 ────────────────────────────────────────────
        quote = msg.get("quote", {})
        if quote:
            quote_type = quote.get("msgtype", "")
            sender = msg.get("from", {}).get("userid", "")
            msg_id = msg.get("msgid", "")
            existing_content = msg.get("_resolved_content", "")
            quote_resolved = None
            if quote_type == "image":
                img = quote.get("image", {})
                url = img.get("url", "")
                aeskey = img.get("aeskey", "")
                if url:
                    quote_resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "image", sender)
            elif quote_type == "file":
                file_info = quote.get("file", {})
                url = file_info.get("url", "")
                aeskey = file_info.get("aeskey", "")
                fname = file_info.get("name", "文件")
                if url:
                    quote_resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "file", sender, fname)
            if quote_resolved:
                # quote 在前，用户消息在后
                if existing_content:
                    msg["_resolved_content"] = f"[引用消息]:\n{quote_resolved}\n\n{existing_content}"
                else:
                    msg["_resolved_content"] = f"[引用消息]:\n{quote_resolved}"

        # ── 群聊 @mention 前缀剥离 ─────────────────────────────────────────
        # 当机器人被 @mention 时，content 可能包含 "@_user_1 " 前缀，
        # 这会导致命令检测（^/）失败。剥离后再送入 core 处理。
        if msg_type == "text":
            import re as _re
            text_content = msg.get("text", {}).get("content", "")
            stripped = _re.sub(r"^@\S+\s+", "", text_content, count=1)
            if stripped != text_content:
                msg["text"] = msg.get("text", {}).copy()
                msg["text"]["content"] = stripped

        inbound = incoming_to_inbound(
            msg,
            bot_id=self.bot_id,
            project_path=self.project_path,
            system_prompt="",
            group_members=None,
            group_context="",
        )

        # 存储 message_id → (user_open_id, bot_id) 映射，供 _handle_tool_call 使用
        self._msg_ctx[inbound.message_id] = (inbound.user_open_id or "", inbound.session_key.bot_id or "")

        # ── 原始入站消息日志（像飞书那样）────────────────────────────────────
        try:
            import json as _json
            raw_body = _json.dumps(msg, ensure_ascii=False, indent=None)
            logger.info(f"[WeComCore] ★ raw inbound: msgtype={msg.get('msgtype')}, msgid={str(msg.get('msgid', ''))[:20]}, from={msg.get('from', {}).get('userid', '?')}, body={raw_body}")
        except Exception:
            logger.info(f"[WeComCore] ★ raw inbound: msgtype={msg.get('msgtype')}, msgid={str(msg.get('msgid', ''))[:20]}, from={msg.get('from', {}).get('userid', '?')}")

        # 保存当前 chat 上下文，供 command_progress 使用
        self._last_chat_id = inbound.session_key.chat_id
        self._last_message_id = inbound.message_id

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
                    platform=inbound.session_key.platform,
                    params={
                        "chat_id": inbound.session_key.chat_id,
                        "user_open_id": inbound.user_open_id or "",
                        "project_path": inbound.session_key.project_path,
                        "content": inbound.content,
                    },
                )
                await self._ws.send(json.dumps(notify_req.to_dict()))
            except Exception:
                pass
            logger.debug(f"[WeComCore] group msg stored, hist_len={len(hist)}")
            return {}

        # ── 群聊上下文 enrichment（历史、成员列表、引用消息）───────────────
        await self._enrich_group_context(inbound, msg)

        # /restart 指令：plugin 本地立即发确认，不等 core 回传
        if inbound.content.strip() == "/restart":
            await self.wecom._ws.reconnect()
            await self.wecom.send_text(inbound.session_key.chat_id, "正在重启，请稍作等待...")

        req = JsonRpcRequest(
            id=self._next_id(),
            method="wecom.message",
            platform=inbound.session_key.platform,
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": inbound.user_open_id,
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

        logger.debug(f"[WeComCore] send_message to core: req.id={req.id}, message_id={inbound.message_id[:20] if inbound.message_id else 'None'}, content={inbound.content[:50] if inbound.content else 'None'}")

        # 所有消息：发给 core 后立即返回，不等执行结果
        # - slash command：结果由 callback 处理（不流式）
        # - 普通对话：结果由 _handle_core_message RESPONSE 事件处理（流式）
        #   callback 只做收尾，在 msg_id in _streamed_msg_ids 时跳过（已流式发送）
        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        self._pending_message_ids[str(req.id)] = (inbound.message_id, inbound.session_key.chat_id)
        logger.debug(f"[WeComCore] stored pending: req.id={req.id} -> (message_id={inbound.message_id[:20] if inbound.message_id else 'None'}, chat_id={inbound.session_key.chat_id})")
        await self._ws.send(json.dumps(req.to_dict()))

        def handle_result(fut: asyncio.Future):
            try:
                result = fut.result()
                self._handle_command_result(inbound, result)
            except Exception as e:
                logger.error(f"[WeComCore] send_message callback error: {e}", exc_info=True)

        future.add_done_callback(handle_result)
        logger.debug(f"[WeComCore] sent, returning immediately (result via callback)")
        return None

    def _handle_command_result(self, inbound, result):
        """处理 command 结果（供 await 和 callback 两条路径共用）。"""
        if not result:
            return
        inner = result.get("result", result)
        result_event = inner.get("event", "") if isinstance(inner, dict) else ""
        result_content = inner.get("content", "") if isinstance(inner, dict) else ""
        if result_event in ("restart", "update"):
            msg_id, chat_id = inbound.message_id, inbound.session_key.chat_id
            if msg_id in self._streamed_msg_ids:
                logger.info("[command] /%s skip (streamed)", result_event)
            else:
                logger.info("[command] /%s forwarding confirmation", result_event)
                asyncio.create_task(self.wecom.send_text(chat_id, result_content or f"正在处理 {result_event}..."))
        elif result_content:
            # 回调结果：如果流式已发送则不重复推送
            # 若流式发送失败，_do_send_text 已从 _streamed_msg_ids 移除 msg_id，此处可补发
            msg_id = inbound.message_id
            if msg_id in self._streamed_msg_ids:
                logger.debug("[command] skip %s (already streamed)", msg_id)
            else:
                chat_id = inbound.session_key.chat_id
                logger.info("[command] sending result via send_markdown, chat_id=%s, content_len=%d", chat_id, len(result_content))
                asyncio.create_task(self.wecom.send_markdown(chat_id, result_content))

    async def _download_and_resolve_media(
        self, msg_id: str, url: str, aeskey: str, msg_type: str, sender: str, file_name: str = ""
    ) -> str | None:
        """下载 WeCom 图片/文件，保存到本地，返回 markdown 格式字符串。

        使用 _media_cache 避免重复下载（同一个 message_id 只下载一次）。
        扩展名优先从 file_name 提取，若无则从 HTTP Content-Disposition 推断。
        """
        if not msg_id or not url:
            return None

        # 命中缓存
        if msg_id in self._media_cache:
            cached = self._media_cache[msg_id]
            if msg_type == "image":
                return f"{sender}: ![image]({cached})"
            else:
                return f"{sender}: [File: {cached}] ({file_name})"

        try:
            import os
            import re as re_module

            data, content_disposition = await self.wecom.download_file(url, aeskey or None)

            # 从 Content-Disposition 提取原始文件名（不含路径，只取basename）
            def _safe_filename(cd: str) -> str:
                if cd:
                    m = re_module.search(r'filename="?([^";\n]+)"?', cd, re_module.IGNORECASE)
                    if m:
                        name = m.group(1)
                        name = name.replace("\\", "/").split("/")[-1]
                        name = re_module.sub(r'[<>:"|?*]', "_", name)
                        return name
                return ""

            # 推断扩展名：优先用 file_name，其次从 Content-Disposition 提取
            def _guess_ext() -> str:
                if file_name:
                    _, ext = os.path.splitext(file_name)
                    if ext and ext != ".":
                        return ext.lower()
                if content_disposition:
                    m = re_module.search(r'filename="?([^";\n]+)"?', content_disposition, re_module.IGNORECASE)
                    if m:
                        _, ext = os.path.splitext(m.group(1))
                        if ext and ext != ".":
                            return ext.lower()
                return ".bin"

            safe_name = _safe_filename(content_disposition)
            ext = _guess_ext()

            # 优先用原始文件名，否则用 msg_id
            base_name = safe_name if safe_name else msg_id
            # 确保有扩展名
            if not os.path.splitext(base_name)[1]:
                base_name = base_name + ext

            data_dir = self._data_dir or ""
            if msg_type == "image":
                images_dir = os.path.join(data_dir, "received_images")
                os.makedirs(images_dir, exist_ok=True)
                save_path = os.path.join(images_dir, base_name)
            else:
                files_dir = os.path.join(data_dir, "received_files")
                os.makedirs(files_dir, exist_ok=True)
                save_path = os.path.join(files_dir, base_name)

            logger.info(f"[WeComCore] downloading {msg_type} to {save_path} (cd={content_disposition[:50] if content_disposition else 'None'})")
            save_bytes(save_path, data)
            self._media_cache[msg_id] = save_path

            if msg_type == "image":
                return f"{sender}: ![image]({save_path})"
            else:
                return f"{sender}: [File: {save_path}] ({file_name})"

        except Exception as e:
            logger.warning(f"[WeComCore] download media failed for {msg_id}: {e}")
            # 下载失败时降级为占位符
            if msg_type == "image":
                return f"{sender}: [图片]"
            else:
                return f"{sender}: [文件: {file_name}]"

    async def _enrich_group_context(self, inbound, msg: dict):
        """为群聊消息收集并注入上下文：历史、mention 规则。

        WeCom API 能力有限（无群成员列表、无历史消息检索），
        仅能从内存历史和 sender 信息构建基本上下文。
        """
        if not inbound.extra.get("is_group_chat"):
            return

        chat_id = inbound.session_key.chat_id
        extra = inbound.extra

        # 群聊历史（从内存）
        # WeCom 存储的是原始 WS 消息字典，需要按 msgtype 提取内容
        # 图片/文件有 url+aeskey，可下载到本地后转为 markdown 路径
        hist = self._group_history.get(chat_id, [])
        if hist:
            history_lines = []
            for h in hist[-10:]:
                sender = h.get("from", {}).get("userid", "?")
                msg_type = h.get("msgtype", "text")
                msg_id = h.get("msgid", "")

                # 提取内容：text 直接用，image/file 下载后转 markdown
                if msg_type == "text":
                    content = h.get("text", {}).get("content", "")
                    if content:
                        history_lines.append(f"{sender}: {content[:200]}")

                elif msg_type == "image":
                    img = h.get("image", {})
                    url = img.get("url", "")
                    aeskey = img.get("aeskey", "")
                    resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "image", sender)
                    if resolved:
                        history_lines.append(resolved)

                elif msg_type == "file":
                    file_info = h.get("file", {})
                    url = file_info.get("url", "")
                    aeskey = file_info.get("aeskey", "")
                    fname = file_info.get("name", "file")
                    resolved = await self._download_and_resolve_media(msg_id, url, aeskey, "file", sender, fname)
                    if resolved:
                        history_lines.append(resolved)

                elif msg_type == "voice":
                    vc = h.get("voice", {})
                    content = vc.get("content", "") if isinstance(vc, dict) else ""
                    if content:
                        history_lines.append(f"{sender}: {content[:200]}")

                else:
                    content = h.get("content", "") or str(h.get("body", {}))
                    if content:
                        history_lines.append(f"{sender}: {content[:200]}")
            if history_lines:
                extra["group_history"] = history_lines

        # mention 规则：企业微信使用 @userid 格式
        sender_open_id = inbound.user_open_id or ""
        if sender_open_id:
            mention_rules = (
                f"【群聊规则】回复时如需引用用户，请使用 @ 语法：@{sender_open_id}。"
                f"企业微信支持在文本中直接使用 @userid 格式。"
            )
            extra["mention_rules"] = mention_rules

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()

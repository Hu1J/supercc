"""Telegram 插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import traceback
from typing import Any

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.channels.telegram.client import TelegramClient
from supercc.channels.telegram.core_protocol import incoming_to_inbound

logger = logging.getLogger("telegram")


class TelegramReplyFormatter:
    """Format tool call results for Telegram (limited card support - no native cards)."""

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
        platform: str = "telegram",
        chat_id: str = "",
    ) -> str:
        """Format a tool call notification as markdown text."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit → diff markdown
        if tool_name == "Edit":
            from supercc.channels.telegram.format.reply_formatter import format_edit_markdown
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                diff_lines = data.get("diff_lines", [])
                return format_edit_markdown(file_path, diff_lines)
            except (json.JSONDecodeError, KeyError):
                return f"{icon} **{short_name}**"

        # Write → diff markdown
        if tool_name == "Write":
            from supercc.channels.telegram.format.reply_formatter import format_write_markdown
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
                header = f"{icon} **{short_name}**"
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
                return f"{icon} **{short_name}** — 所有任务已完成"

            status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
            rows = ["| 状态 | 待办事项 |", "|------|----------|"]
            for t in todos:
                icon_s = status_icon.get(t.get("status", "pending"), "⬜")
                content = str(t.get("content", "")).replace("\n", " ")
                rows.append(f"| {icon_s} | {content} |")
            return f"{icon} **{short_name}**\n\n" + "\n".join(rows)

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


class StreamAccumulator:
    """Buffers streaming text chunks and flushes to Telegram in batches."""

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


class TelegramCoreWSClient:
    """
    Telegram 插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 Telegram 消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为 Telegram 格式并发送

    不负责：Session 管理、AI 推理
    """

    def __init__(
        self,
        core_url: str,
        ws_client,  # TelegramWSClient
        telegram_client: TelegramClient,
        bot_id: str,
        project_path: str,
        groups: dict | None = None,
        allowed_users: list | None = None,
    ):
        self.core_url = core_url
        self.ws_client = ws_client
        self.telegram = telegram_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._groups = groups or {}
        self._allowed_users = allowed_users or []
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._pending_message_ids: dict[str, tuple[str, str]] = {}
        self._id_counter = 0
        self._streamed_msg_ids: set[str] = set()
        self._accumulator_by_msg_id: dict[str, StreamAccumulator] = {}
        self._last_chat_id: str = ""
        self._last_message_id: str = ""
        self._group_history: dict[str, list[dict]] = {}
        self._MAX_GROUP_HISTORY = 10

        self.formatter = TelegramReplyFormatter()
        self._reconnect_lock = asyncio.Lock()

        # Memory Manager (MCP tool executor)
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
                "platform": "telegram"
            }))
        elif cfg.core.username and cfg.core.password:
            await self._ws.send(json.dumps({
                "type": "auth",
                "username": cfg.core.username,
                "password": cfg.core.password,
                "platform": "telegram"
            }))

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("Connected to core")
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
                    logger.warning("Connection closed, reconnecting...")
                    await self._reconnect()
                else:
                    break
            except Exception:
                logger.error("Error reading message\n%s", traceback.format_exc())

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
            logger.info("Reconnected to core")
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
                    {"jsonrpc": "2.0", "id": req_id, "method": "core.ping", "params": {}, "platform": "telegram"}
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
        if "id" in data:
            req_id = str(data.get("id"))
            stored = self._pending_message_ids.pop(req_id, None)
            if stored:
                msg_id, chat_id = stored
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

            # Group mention: append @username
            if is_group and sender_id:
                username = extra.get("username", "")
                if username and f"@{username}" not in content:
                    params["content"] = content + f" @{username}"

            await self._render_and_send(params)

            if session_info:
                chat_id = params.get("chat_id", "")
                await self.telegram.send_text(chat_id, session_info)

        elif method == Event.STREAM_CHUNK:
            await self._render_and_send(params)

        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)

        elif method == "restart":
            chat_id = params.get("chat_id", "")
            msg_id = params.get("message_id", "")
            content = params.get("content", "正在重启...")
            if content:
                await self.telegram.send_text(chat_id, content)

        elif method == Event.PONG:
            pass

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
                await self.telegram.send_text(chat_id, content)

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为 Telegram 格式并发送。"""
        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")
        event = params.get("event", "")

        if not content:
            return

        # Non-streaming events (restart/update) don't go through accumulator
        if message_id and event in ("restart", "update"):
            self._streamed_msg_ids.discard(message_id)
            self._accumulator_by_msg_id.pop(message_id, None)
            await self.telegram.send_text(chat_id, content)
            return

        # Buffer text chunks
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
                    send_fn=lambda cid, mid, text: self._do_send_text(cid, text, mid),
                )
                await self._accumulator_by_msg_id[message_id].add_text(content)
        else:
            # No message_id - send directly
            await self.telegram.send_text(chat_id, content)

    async def _do_send_text(self, chat_id: str, text: str, message_id: str) -> None:
        """Send text to Telegram."""
        try:
            await self.telegram.send_text(chat_id, text, reply_to=message_id)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")

    async def _handle_command_progress(self, params: dict):
        """Handle restart/update progress messages."""
        event = params.get("event", "")
        step = params.get("step", 0)
        total = params.get("total", 0)
        status = params.get("status", "")
        detail = params.get("detail", "")
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
            title_done = f"✅ {event} 完成"

        if status == "final":
            if event == "restart":
                body = f"新进程 PID: {new_pid}\n\nSuperCC 已重启，可以在 Telegram 中继续对话了。"
            elif event == "update":
                body = "SuperCC 已更新，可以在 Telegram 中继续对话了。"
            else:
                body = detail
            text = f"{title_done}\n\n{body}"
        else:
            step_labels = {
                "restart": ["🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
                "update": ["📋 检查更新", "📦 检查新版本", "✅ 下载完成", "🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成"],
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
            await self.telegram.send_text(chat_id, text)
        except Exception as e:
            logger.warning("[command_progress] send failed: %s", e)

    async def _handle_cron_progress(self, params: dict):
        """Handle cron intermediate progress messages."""
        content = params.get("content", "")
        chat_id = params.get("chat_id") or self._last_chat_id or ""

        if not chat_id:
            logger.warning("[cron_progress] no chat_id, skipping")
            return

        if not isinstance(content, str):
            content = str(content)

        try:
            await self.telegram.send_text(chat_id, content)
        except Exception as e:
            logger.warning(f"[cron_progress] failed to send: {e}")

    async def _handle_cron_result(self, params: dict):
        """Handle cron final result messages."""
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
                await self.telegram.send_text(chat_id, text)
            else:
                await self.telegram.send_text(chat_id, content)
        except Exception as e:
            logger.warning(f"[cron_result] failed to send: {e}")

    async def _handle_tool_call(self, params: dict):
        """Handle tool call events from core."""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # Flush any pending streaming text
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        result = self.formatter.format_tool_call(
            tool_name, tool_input,
            memory_manager=self._memory_manager,
            default_project_path=self.project_path,
            platform="telegram",
            chat_id=chat_id,
        )

        # Send formatted result
        text = str(result) if result else ""
        if text:
            try:
                await self.telegram.send_text(chat_id, text, reply_to=msg_id)
            except Exception as e:
                logger.warning(f"Failed to send tool call result: {e}")

    async def _check_group_permissions(self, inbound) -> bool:
        """Check group chat permissions. Returns True=allow, False=block."""
        is_group = inbound.extra.get("is_group_chat", False)

        if is_group:
            entry = self._groups.get(inbound.session_key.chat_id)
            if entry is None:
                reason = "该群未配置使用权限，请联系管理员。"
                try:
                    await self.telegram.send_text(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            if not getattr(entry, "enabled", True):
                reason = "该群已被禁用。"
                try:
                    await self.telegram.send_text(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            # For Telegram, mentions are detected via @username in text
            # For now, groups require mention to respond (require_mention=True by default)
            if getattr(entry, "require_mention", True) and not inbound.extra.get("mention_bot", False):
                # Check if bot was mentioned in message content
                content = inbound.content
                bot_username = getattr(self, "_bot_username", "")
                if bot_username and f"@{bot_username}" not in content:
                    return False
                return True

            allow_from = getattr(entry, "allow_from", [])
            if allow_from and inbound.user_open_id not in allow_from:
                reason = "你在该群中没有使用权限。"
                try:
                    await self.telegram.send_text(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False

            return True
        else:
            # P2P: check allowed_users whitelist + pairing system
            user_id = inbound.user_open_id
            from supercc.config import reload_config
            cfg = reload_config()
            channel_cfg = getattr(cfg.channels, "telegram", None)
            allowed_users = list(getattr(channel_cfg, "allowed_users", [])) if channel_cfg else []

            do_pairing_check = not allowed_users or user_id not in allowed_users
            if do_pairing_check:
                try:
                    from supercc.core.pairing import get_pairing_store
                    store = get_pairing_store()
                    username = inbound.extra.get("username", "")
                    code = store.generate_code("telegram", user_id, username)
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
                    await self.telegram.send_text(inbound.session_key.chat_id, reason)
                except Exception:
                    pass
                return False
            return True

    async def send_message(self, msg: dict) -> dict:
        """将 Telegram 消息转发给核心，并等待响应。"""
        import re as _re

        # Save current chat context
        self._last_chat_id = msg.get("chat_id", "")
        self._last_message_id = msg.get("msgid", "")

        # Parse incoming message to inbound
        inbound = incoming_to_inbound(
            msg,
            bot_id=self.bot_id,
            project_path=self.project_path,
            system_prompt="",
            group_members=None,
            group_context="",
        )

        # Strip @mention prefix from content for group chats
        if inbound.extra.get("is_group_chat") and inbound.message_type.value == "text":
            content = inbound.content
            # Strip @username patterns
            stripped = _re.sub(r"@\w+\s+", "", content, count=1)
            if stripped != content:
                inbound = inbound.__class__(
                    event=inbound.event,
                    session_key=inbound.session_key,
                    message_id=inbound.message_id,
                    role=inbound.role,
                    content=stripped,
                    message_type=inbound.message_type,
                    media_path=inbound.media_path,
                    user_open_id=inbound.user_open_id,
                    thread_id=inbound.thread_id,
                    timestamp=inbound.timestamp,
                    extra=inbound.extra,
                    system_prompt=inbound.system_prompt,
                    group_context=inbound.group_context,
                )

        # Group permissions check
        if not await self._check_group_permissions(inbound):
            return {}

        # Group non-@mention messages: store in history, notify core
        if inbound.extra.get("is_group_chat") and not inbound.extra.get("mention_bot"):
            hist = self._group_history.setdefault(inbound.session_key.chat_id, [])
            hist.append(msg)
            if len(hist) > self._MAX_GROUP_HISTORY:
                hist[:] = hist[-self._MAX_GROUP_HISTORY:]
            # Send lightweight notification to core
            try:
                notify_req = JsonRpcRequest(
                    id=self._next_id(),
                    method="telegram.notify",
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
            return {}

        # /restart command: send immediate confirmation
        if inbound.content.strip() == "/restart":
            await self.telegram.send_text(inbound.session_key.chat_id, "正在重启，请稍作等待...")

        req = JsonRpcRequest(
            id=self._next_id(),
            method="telegram.message",
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

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        self._pending_message_ids[str(req.id)] = (inbound.message_id, inbound.session_key.chat_id)

        await self._ws.send(json.dumps(req.to_dict()))
        result = await future

        # Handle restart/update confirmation
        if result:
            inner = result.get("result", result)
            result_event = inner.get("event", "") if isinstance(inner, dict) else ""
            result_content = inner.get("content", "") if isinstance(inner, dict) else ""
            if result_event in ("restart", "update"):
                msg_id, chat_id = inbound.message_id, inbound.session_key.chat_id
                if msg_id in self._streamed_msg_ids:
                    logger.info("[command] /%s skip (streamed)", result_event)
                else:
                    logger.info("[command] /%s forwarding confirmation", result_event)
                    await self.telegram.send_text(chat_id, result_content or f"正在处理 {result_event}...")

        return result or {}

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()
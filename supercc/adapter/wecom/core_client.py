"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

from supercc.core.protocol import JsonRpcRequest, Event
from supercc.adapter.feishu.media import save_bytes
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable
from wecom_aibot_sdk import generate_req_id

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

    def format_tool_call(self, tool_name: str, tool_input: str | None = None) -> str:
        """Format a tool call notification as markdown text."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit/Write → code block
        if tool_name in ("Edit", "Write"):
            if tool_input.strip():
                try:
                    data = json.loads(tool_input)
                    file_path = data.get("file_path", "unknown")
                    return f"{icon} **{short_name}** — `{file_path}`"
                except json.JSONDecodeError:
                    pass
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
        # 媒体缓存：message_id → 本地保存路径（避免重复下载）
        self._media_cache: dict[str, str] = {}

        # WeCom 格式化管线
        self.formatter = WeComReplyFormatter()

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
                logger.error("[WeComCore] Error reading message\n%s", traceback.format_exc())

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
        """Send text to WeCom with three-level fallback (called by StreamAccumulator)."""
        # 检测是否包含飞书特有的 card 标记（从 Feishu 迁移的内容）
        is_card_content = "<at user_id=" in text or "```" in text or "## " in text

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
                    return
            except Exception as e:
                logger.warning(f"[WeComCore] reply_stream failed: {e}")

        # 三级降级
        if is_card_content:
            try:
                await self.wecom.send_template_card(
                    chat_id=chat_id,
                    card_type="text_notice",
                    title="消息",
                    desc=text[:500],
                )
                return
            except Exception:
                pass

        try:
            await self.wecom.send_markdown(chat_id, text)
        except Exception:
            try:
                await self.wecom.send_text(chat_id, text[:2000])
            except Exception as e:
                logger.warning(f"[WeComCore] all send methods failed: {e}")

    async def _handle_tool_call(self, params: dict):
        """tool_call 事件：格式化工具结果并发送给用户。"""
        extra = params.get("extra", {})
        tool_name = extra.get("tool_name", "")
        tool_input_raw = extra.get("tool_input", "")
        tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw or "")
        tool_call_id = params.get("tool_call_id", "") or extra.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")
        msg_id = params.get("message_id", "")

        # Flush any pending streaming text for this message before handling tool call
        if msg_id and msg_id in self._accumulator_by_msg_id:
            acc = self._accumulator_by_msg_id[msg_id]
            await acc.flush()

        # 格式化工具调用通知（TOOL_RESULT 回给 core 做记录，用户通知由 core 的 WS 推送）
        result_content = self.formatter.format_tool_call(tool_name, tool_input)
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

        inbound = incoming_to_inbound(
            msg,
            bot_id=self.bot_id,
            project_path=self.project_path,
            system_prompt="",
            group_members=None,
            group_context="",
        )

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

    async def _download_and_resolve_media(
        self, msg_id: str, url: str, aeskey: str, msg_type: str, sender: str, file_name: str = ""
    ) -> str | None:
        """下载 WeCom 图片/文件，保存到本地，返回 markdown 格式字符串。

        使用 _media_cache 避免重复下载（同一个 message_id 只下载一次）。
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
            import tempfile
            import os

            data, _ = await self.wecom.download_file(url, aeskey or None)

            # 保存到 temp 目录
            tmp_dir = os.path.join(tempfile.gettempdir(), "supercc-wecom-media")
            os.makedirs(tmp_dir, exist_ok=True)

            if msg_type == "image":
                ext = ".png"
                save_path = os.path.join(tmp_dir, f"{msg_id}{ext}")
            else:
                # file: 保留原扩展名
                if file_name:
                    _, ext = os.path.splitext(file_name)
                    if not ext or ext == ".":
                        ext = ".bin"
                else:
                    ext = ".bin"
                save_path = os.path.join(tmp_dir, f"{msg_id}{ext}")

            logger.info(f"[WeComCore] downloading {msg_type} to {save_path}")
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

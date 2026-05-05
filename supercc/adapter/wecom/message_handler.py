"""Message handler orchestrator — routes messages to Claude and back."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from supercc.adapter.wecom.client import WeComClient, WeComIncomingMessage
from supercc.adapter.wecom.format.agent_card import format_agent_card, format_codex_card
from supercc.adapter.wecom.format.edit_diff import _DiffMarker, _MemoryCardMarker
from supercc.adapter.wecom.format.questionnaire_card import _AskUserQuestionMarker, format_questionnaire_card
from supercc.adapter.wecom.format.reply_formatter import ReplyFormatter
from supercc.claude.codex_exec import CodexStreamEvent
from supercc.claude.codex_mcp import (
    ensure_codex_mcp_configured,
    format_codex_availability,
    format_codex_models,
    format_codex_status,
    get_codex_mcp_guide,
    get_codex_mcp_status,
)
from supercc.claude.integration import ClaudeIntegration
from supercc.claude.message_context import get_current_platform, set_current_context
from supercc.claude.memory_manager import get_memory_manager, MEMORY_SYSTEM_GUIDE
from supercc.claude.session_manager import SessionManager
from supercc.security.auth import Authenticator
from supercc.security.validator import SecurityValidator

logger = logging.getLogger(__name__)


_COMMAND_RE = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_-]*(?:\s.*)?$")
_CONFIG_RELOAD_NAMES = {"config.yaml", "config.json"}


def _is_command(text: str) -> bool:
    return bool(_COMMAND_RE.match(text))


def _strip_mention_prefix(content: str) -> str:
    return re.sub(r"^@_user_\d+\s*", "", content)


@dataclass
class HandlerResult:
    success: bool
    response_text: str | None = None
    error: str | None = None


class StreamAccumulator:
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


class SessionWorker:
    def __init__(self, chat_id: str, handler: "MessageHandler"):
        import time
        self.chat_id = chat_id
        self.queue: asyncio.Queue[WeComIncomingMessage] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.handler = handler
        self._cwd = handler.approved_directory
        self.claude = ClaudeIntegration(
            cli_path=handler.config.claude.cli_path,
            max_turns=50,
            approved_directory=self._cwd,
        )
        self.claude_memory = ClaudeIntegration(
            cli_path=handler.config.claude.cli_path,
            max_turns=5,
            approved_directory=self._cwd,
            memory_only=True,
        )
        self.claude_skill = ClaudeIntegration(
            cli_path=handler.config.claude.cli_path,
            max_turns=5,
            approved_directory=self._cwd,
        )
        self._running = False
        self._idle_since: float | None = None
        self.IDLE_TIMEOUT = 604800
        self._is_first_session: bool = True
        self._current_user_open_id: str | None = None
        self._current_chat_id: str | None = None
        self._current_platform: str = "wecom"
        self._sdk_session_id: str | None = None

    def _trigger_memory_review(self, message: WeComIncomingMessage, response_text: str) -> None:
        logger.info("[_trigger_memory_review] starting background review")
        h = self.handler
        prompt = (
            "根据之前的对话，判断是否有值得记住的信息。需要时直接调用 MCP 工具（新增/更新/删除）来管理记忆，不需要问我任何问题。\n"
        )
        async def do_review():
            if self.claude_memory._options is None:
                self.claude_memory._init_options()
            async def stream_callback(claude_msg):
                if claude_msg.tool_name and claude_msg.tool_name.startswith("mcp__SuperCC__Memory"):
                    result = h.formatter.format_tool_call(
                        claude_msg.tool_name, claude_msg.tool_input,
                        memory_manager=h.memory_manager,
                        default_project_path=getattr(h, "_current_project_path", ""),
                        platform=get_current_platform(),
                        chat_id=message.chat_id or "",
                    )
                    if isinstance(result, _MemoryCardMarker):
                        await h._safe_send(message.chat_id, message.message_id, result.render())
                    else:
                        await h._safe_send(message.chat_id, message.message_id, result)
                    logger.info(f"[memory_review] tool: {claude_msg.tool_name}")
            try:
                await self.claude_memory.query(prompt=prompt, on_stream=stream_callback)
            except Exception as e:
                logger.warning(f"[_trigger_memory_review] failed: {e}")
            finally:
                logger.info("[_trigger_memory_review] done.")
        asyncio.create_task(do_review())

    async def _run_loop(self) -> None:
        import time
        self._running = True
        while True:
            try:
                try:
                    message = await asyncio.wait_for(self.queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    if self._idle_since is None:
                        self._idle_since = time.time()
                    elif time.time() - self._idle_since > self.IDLE_TIMEOUT:
                        break
                    continue
                self._idle_since = None
                try:
                    await self._process_message(message)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(f"Worker {self.chat_id} error")
        self._running = False

    async def _process_message(self, message: WeComIncomingMessage) -> None:
        h = self.handler
        self._current_user_open_id = message.user_open_id
        self._current_chat_id = message.chat_id
        self._current_platform = "wecom"
        set_current_context(message.user_open_id, message.chat_id, "wecom")

        if not message.is_group_chat:
            auth_result = h.auth.authenticate(message.user_open_id)
            if not auth_result.authorized:
                logger.info(f"Ignoring message from unauthorized user: {message.user_open_id}")
                return

        if not await h._check_group_access(message):
            return

        if message.message_type not in ("text", "image", "file"):
            await h._safe_send(message.chat_id, message.message_id, "暂不支持该消息类型，请发送文字消息。")
            return

        if message.is_group_chat:
            session = h.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="wecom")
            if session is None:
                session = h.sessions.create_session(
                    message.user_open_id, h.approved_directory,
                    chat_id=message.chat_id, platform="wecom",
                )
        else:
            session = h.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="wecom")
            if session is None:
                session = h.sessions.create_session(
                    message.user_open_id, h.approved_directory,
                    chat_id=message.chat_id, platform="wecom",
                )

        project_path = session.project_path if session else h.approved_directory
        h._current_project_path = project_path

        agents_md_path = os.path.join(project_path, "AGENTS.md")
        agents_md_content = ""
        if os.path.isfile(agents_md_path):
            try:
                with open(agents_md_path, encoding="utf-8") as f:
                    agents_md_content = f.read().strip()
            except Exception:
                pass

        codex_status = get_codex_mcp_status(h.config.codex)
        system_prompt_append = (
            (agents_md_content + "\n\n") if agents_md_content else ""
        ) + (
            MEMORY_SYSTEM_GUIDE
            + get_codex_mcp_guide(h.config.codex, codex_status)
            + h.memory_manager.inject_context(
                user_open_id=message.user_open_id,
                project_path=project_path,
                platform=get_current_platform(),
                chat_id=message.chat_id,
            )
        )

        self._init_options(system_prompt_append)
        await self._run_query(message, session)

    def _init_options(self, system_prompt_append: str | None = None, continue_conversation: bool = True) -> None:
        if self._is_first_session:
            self._is_first_session = False
            self._sdk_session_id = None
        resume_id = self._sdk_session_id if self._sdk_session_id else None
        self.claude._init_options(
            system_prompt_append,
            continue_conversation=False,
            channel="wecom",
            session_id=None,
            resume=resume_id,
        )

    async def _run_query(self, message: WeComIncomingMessage, session) -> None:
        h = self.handler
        _last_response = ""

        try:
            if message.message_type == "audio":
                await h._safe_send(message.chat_id, message.message_id, "🎙️ 暂不支持语音消息，请发送文字消息。")
                return

            media_prompt_prefix = ""
            media_notify_text = ""
            if message.message_type in ("image", "file"):
                try:
                    media_prompt_prefix = await h._preprocess_media(message)
                    if media_prompt_prefix:
                        logger.info(f"Inbound media saved: {media_prompt_prefix}")
                        orig_name = ""
                        _m = re.search(r"\]\s*\(([^)]+)\)\s*$", media_prompt_prefix)
                        if _m:
                            orig_name = _m.group(1)
                        if orig_name and orig_name.lower() in _CONFIG_RELOAD_NAMES:
                            logger.info(f"[config-reload] detected config upload: {orig_name}")
                            try:
                                from supercc.config import init_config, resolve_config_path
                                cfg_path, data_dir = resolve_config_path()
                                init_config(cfg_path, data_dir)
                                await h._safe_send(
                                    message.chat_id, message.message_id,
                                    "⚙️ 配置文件已更新，将在当前查询中生效。"
                                )
                            except Exception as cfg_err:
                                logger.warning(f"[config-reload] failed: {cfg_err}")
                                await h._safe_send(
                                    message.chat_id, message.message_id,
                                    f"⚠️ 配置文件已保存，但重载失败：{cfg_err}"
                                )
                        else:
                            icon = {"image": "🖼️", "file": "🗃"}.get(message.message_type, "🗃")
                            media_notify_text = f"{icon} 收到 {message.message_type}，正在分析..."
                            await h._safe_send(message.chat_id, message.message_id, media_notify_text)
                except Exception as e:
                    logger.warning(f"Failed to process inbound media: {e}")
                    media_prompt_prefix = ""

            quoted_content = ""
            if message.parent_id:
                try:
                    quoted_msg = await h.wecom.get_message(message.parent_id)
                    if quoted_msg:
                        sender_id = quoted_msg.get("sender_id", "")
                        quoted_text = h._extract_quoted_content(quoted_msg)
                        if sender_id == message.user_open_id:
                            quoted_content = ""
                        else:
                            quoted_content = f"[引用消息: {message.parent_id}] {quoted_text}"
                except Exception:
                    quoted_content = f"[引用消息不可用: {message.parent_id}]"

            group_history_prefix = ""
            if message.is_group_chat and message.chat_id:
                hist = h._group_history.get(message.chat_id, [])
                if hist:
                    history_text = "\n".join(hist)
                    group_history_prefix = f"[群聊上下文]\n{history_text}\n\n"

            prefix_parts = [p for p in [group_history_prefix, media_prompt_prefix, quoted_content] if p]
            prefix = "\n".join(prefix_parts) + "\n" if prefix_parts else ""
            is_media = message.message_type in ("image", "file")
            if is_media and media_prompt_prefix:
                user_text = message.content.strip()
                if user_text:
                    full_prompt = (prefix + user_text).strip()
                else:
                    full_prompt = prefix.strip()
            else:
                full_prompt = (prefix + message.content).strip()

            last_cost = 0.0
            _stream_too_long = [False]
            for retry_round in range(3):
                accumulator = StreamAccumulator(message.chat_id, message.message_id, h._safe_send)

                async def stream_callback(stream_item):
                    if isinstance(stream_item, CodexStreamEvent):
                        await accumulator.flush()
                        text = h._format_codex_event(stream_item)
                        if text:
                            card = format_codex_card(
                                stream_item.type, text,
                                {"command": stream_item.command, "exit_code": stream_item.exit_code, "tool_name": stream_item.tool_name},
                            )
                            await h._safe_send(message.chat_id, message.message_id, card.get("content", ""))
                        return

                    claude_msg = stream_item
                    if _is_codex_tool_name(claude_msg.tool_name):
                        await accumulator.flush()
                        card = format_agent_card(claude_msg.tool_input or "", title="## 🤖 Codex")
                        await h._safe_send(message.chat_id, message.message_id, card.get("content", ""))
                        return

                    if claude_msg.tool_name:
                        await accumulator.flush()
                        kwargs = {}
                        if claude_msg.tool_name.startswith("mcp__SuperCC__Memory"):
                            kwargs["memory_manager"] = h.memory_manager
                            kwargs["default_project_path"] = getattr(h, "_current_project_path", "")
                            kwargs["platform"] = get_current_platform()
                            kwargs["chat_id"] = message.chat_id or ""
                        result = h.formatter.format_tool_call(
                            claude_msg.tool_name, claude_msg.tool_input, **kwargs,
                        )
                        logger.info(f"[stream] tool: {claude_msg.tool_name} | input: {claude_msg.tool_input}")

                        if isinstance(result, _DiffMarker):
                            await h._safe_send(message.chat_id, message.message_id, result.render(), log_reply=False)
                        elif isinstance(result, list):
                            for marker in result:
                                if isinstance(marker, _DiffMarker):
                                    await h._safe_send(message.chat_id, message.message_id, marker.render(), log_reply=False)
                        elif isinstance(result, _MemoryCardMarker):
                            await h._safe_send(message.chat_id, message.message_id, result.render(), log_reply=False)
                        elif isinstance(result, _AskUserQuestionMarker):
                            if result.data is not None:
                                card = format_questionnaire_card(result)
                                await h._safe_send(message.chat_id, message.message_id, card.get("content", ""), log_reply=False)
                            else:
                                await h._safe_send(message.chat_id, message.message_id, f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`", log_reply=False)
                        else:
                            await h._safe_send(message.chat_id, message.message_id, result, log_reply=False)
                    elif claude_msg.content:
                        logger.info(f"[stream] text: {claude_msg.content[:100]}")
                        await accumulator.add_text(claude_msg.content)
                        _content_lower = claude_msg.content.lower()
                        if any(x in _content_lower for x in ("too long", "超出", "context window", "context_length", "max_tokens")):
                            _stream_too_long[0] = True

                response, sdk_session_id_from_query, cost = await self.claude.query(
                    prompt=full_prompt,
                    on_stream=stream_callback,
                )
                last_cost = cost
                await accumulator.flush()

                if accumulator.sent_something or response:
                    _last_response = response or ""
                    if _stream_too_long[0]:
                        await h._safe_send(
                            message.chat_id, message.message_id,
                            "💡 上下文已满，发送 **/new** 可开启新会话，我会记住之前的进度。",
                            log_reply=False,
                        )
                    break
                if retry_round < 2:
                    logger.warning(f"[_run_query] Empty response (cost={cost}), retrying ({retry_round + 1}/3)")
            else:
                logger.error("[_run_query] 3 次重试均失败，放弃查询")
                await h._safe_send(
                    message.chat_id, message.message_id,
                    "⚠️ 查询失败：SDK 返回空响应，请稍后重试。"
                )
                return

            if not session:
                session = h.sessions.create_session(
                    message.user_open_id, h.approved_directory,
                    sdk_session_id=sdk_session_id_from_query,
                    chat_id=message.chat_id, platform="wecom",
                )
            else:
                h.sessions.update_session(session.session_id, cost=last_cost, message_increment=1, update_last_message=True)

            new_sid = (sdk_session_id_from_query or "").strip()
            if new_sid:
                self._sdk_session_id = new_sid
            if new_sid:
                old_sid = (session.sdk_session_id or "").strip()
                if new_sid != old_sid:
                    logger.info(f"[_run_query] sdk_session_id: {old_sid!r} -> {new_sid!r}")
                    h.sessions.update_sdk_session_id(session.session_id, new_sid)
                    if old_sid:
                        await h._safe_send(
                            message.chat_id, message.message_id,
                            f"🔄 已切换到新 Session\nSession ID: `{new_sid}`",
                            log_reply=False,
                        )
                    else:
                        await h._safe_send(
                            message.chat_id, message.message_id,
                            f"✅ 新 Session 已建立\nSession ID: `{new_sid}`",
                            log_reply=False,
                        )

        except asyncio.CancelledError:
            await h._safe_send(message.chat_id, message.message_id, "🛑 已打断 Claude。")
        except Exception as e:
            logger.exception(f"Error in _run_query: {e}")
            await h._safe_send(message.chat_id, message.message_id, f"⚠️ 内部错误：{e}")
        finally:
            self._trigger_memory_review(message, _last_response)


class MessageHandler:
    def __init__(
        self,
        wecom_client: WeComClient,
        authenticator: Authenticator,
        validator: SecurityValidator,
        claude: ClaudeIntegration,
        session_manager: SessionManager,
        formatter: ReplyFormatter,
        approved_directory: str,
        config=None,
        data_dir: str = "",
        wecom_groups: dict | None = None,
        config_path: str | None = None,
    ):
        if config is None:
            from supercc.config import AuthConfig, ChannelsConfig, ClaudeConfig, Config
            config = Config(
                channels=ChannelsConfig(),
                auth=AuthConfig(),
                claude=ClaudeConfig(approved_directory=approved_directory),
            )
        self.wecom = wecom_client
        self.auth = authenticator
        self.validator = validator
        self.claude = claude
        self.claude_memory = ClaudeIntegration(
            cli_path=config.claude.cli_path,
            max_turns=5,
            approved_directory=approved_directory,
            memory_only=True,
        )
        self.sessions = session_manager
        self.formatter = formatter
        self.approved_directory = approved_directory
        self.data_dir = data_dir
        self.config = config
        self._wecom_groups = wecom_groups or {}
        self._config_path = config_path
        self.memory_manager = get_memory_manager()
        self.memory_manager.set_system_prompt_stale_callback(self._noop_mark_stale)
        self._group_history: dict[str, list[str]] = {}
        self._fetched_group_chats: set[str] = set()
        self._session_workers: dict[str, SessionWorker] = {}
        self._workers_lock = asyncio.Lock()
        self._max_concurrent_workers = 50
        self._current_message_id: str = ""

    def _noop_mark_stale(self) -> None:
        pass

    def _trigger_memory_review(self, message: WeComIncomingMessage, response_text: str) -> None:
        logger.info("[_trigger_memory_review] starting background review")
        prompt = (
            "根据之前的对话，判断是否有值得记住的信息。需要时直接调用 MCP 工具（新增/更新/删除）来管理记忆，不需要问我任何问题。\n"
        )
        async def do_review():
            if self.claude_memory._options is None:
                self.claude_memory._init_options()
            async def stream_callback(claude_msg):
                if claude_msg.tool_name and claude_msg.tool_name.startswith("mcp__SuperCC__Memory"):
                    result = self.formatter.format_tool_call(
                        claude_msg.tool_name, claude_msg.tool_input,
                        memory_manager=self.memory_manager,
                        default_project_path=getattr(self, "_current_project_path", ""),
                        platform=get_current_platform(),
                        chat_id=message.chat_id or "",
                    )
                    if isinstance(result, _MemoryCardMarker):
                        await self._safe_send(message.chat_id, message.message_id, result.render())
                    else:
                        await self._safe_send(message.chat_id, message.message_id, result)
                    logger.info(f"[memory_review] tool: {claude_msg.tool_name}")
            try:
                await self.claude_memory.query(prompt=prompt, on_stream=stream_callback)
            except Exception as e:
                logger.warning(f"[_trigger_memory_review] failed: {e}")
            finally:
                logger.info("[_trigger_memory_review] done.")
        asyncio.create_task(do_review())

    async def _get_group_config(self, chat_id: str):
        if chat_id in self._wecom_groups:
            return self._wecom_groups[chat_id]
        from supercc.config import GroupConfigEntry, register_group_config
        entry = GroupConfigEntry()
        self._wecom_groups[chat_id] = entry
        if self._config_path:
            try:
                await asyncio.to_thread(register_group_config, self._config_path, chat_id, entry)
                logger.info(f"Auto-registered new group {chat_id} in config")
            except Exception as ex:
                logger.warning(f"Failed to auto-register group {chat_id} in config: {ex}")
        return entry

    async def _check_group_access(self, message: WeComIncomingMessage) -> bool:
        if not message.is_group_chat:
            return True
        if not message.chat_id:
            logger.warning("Group chat message has empty chat_id, skipping")
            return False
        group_cfg = await self._get_group_config(message.chat_id)
        if group_cfg and not group_cfg.enabled:
            logger.info(f"Group {message.chat_id} is disabled in config, skipping")
            return False
        if group_cfg and group_cfg.allow_from:
            if message.user_open_id not in group_cfg.allow_from:
                logger.info(f"User {message.user_open_id} not in group allow_from for {message.chat_id}, skipping")
                return False
        if group_cfg and not group_cfg.require_mention:
            return True
        if not message.mention_bot:
            logger.info(f"Group chat message in {message.chat_id} without @mention, skipping")
            return False
        return True

    async def handle(self, message: WeComIncomingMessage) -> HandlerResult:
        if message.is_group_chat and message.content:
            if message.chat_id not in self._fetched_group_chats:
                self._fetched_group_chats.add(message.chat_id)
            hist = self._group_history.setdefault(message.chat_id, [])
            hist.append(f"{message.user_open_id}: {message.content}")
            if len(hist) > 20:
                hist[:] = hist[-20:]

        if message.is_group_chat and not message.mention_bot:
            logger.info(f"Group command without @mention in {message.chat_id}, skipping")
            return HandlerResult(success=True)

        content = _strip_mention_prefix(message.content)
        if content.startswith("/") and _is_command(content):
            auth_result = self.auth.authenticate(message.user_open_id)
            if not auth_result.authorized:
                logger.info(f"Ignoring command from unauthorized user: {message.user_open_id}")
                return HandlerResult(success=True)
            result = await self._handle_command(message)
            if result.response_text:
                await self._safe_send(message.chat_id, message.message_id, result.response_text)
            return HandlerResult(success=True)

        worker = await self._get_or_create_worker(message.chat_id)
        await worker.queue.put(message)
        return HandlerResult(success=True)

    async def _get_or_create_worker(self, chat_id: str) -> SessionWorker:
        async with self._workers_lock:
            if chat_id not in self._session_workers:
                active = [w for w in self._session_workers.values() if w._running]
                if len(active) >= self._max_concurrent_workers:
                    idle_workers = [w for w in active if w._idle_since is not None]
                    if idle_workers:
                        oldest = min(idle_workers, key=lambda w: w._idle_since)
                    else:
                        oldest = active[0]
                    old_chat_id = oldest.chat_id
                    logger.warning(
                        f"[WORKER_LIMIT] chat_id={chat_id} 淘汰 worker={old_chat_id} "
                        f"(active={len(active)}, max={self._max_concurrent_workers})"
                    )
                    if oldest.task and not oldest.task.done():
                        oldest.task.cancel()
                    if old_chat_id in self._session_workers:
                        del self._session_workers[old_chat_id]
                    self._session_workers[chat_id] = SessionWorker(chat_id, self)
                else:
                    self._session_workers[chat_id] = SessionWorker(chat_id, self)
            worker = self._session_workers[chat_id]
            if worker.task is None or worker.task.done():
                worker.task = asyncio.create_task(worker._run_loop())
            return worker

    async def _handle_command(self, message: WeComIncomingMessage) -> HandlerResult:
        content = _strip_mention_prefix(message.content)
        parts = content.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/new":
            session = self.sessions.create_session(
                message.user_open_id, self.approved_directory,
                chat_id=message.chat_id, platform="wecom",
            )
            worker = await self._get_or_create_worker(message.chat_id)
            worker._is_first_session = True
            worker._sdk_session_id = None
            return HandlerResult(
                success=True,
                response_text=f"✅ 新会话已创建\n会话ID: {session.session_id}\n工作目录: {session.project_path}",
            )
        elif cmd == "/status":
            return await self._handle_status(message)
        elif cmd == "/stop":
            return await self._handle_stop(message)
        elif cmd == "/help":
            return HandlerResult(
                success=True,
                response_text=(
                    "supercc 命令：\n"
                    "• /new — 新建会话\n"
                    "• /status — 会话状态\n"
                    "• /stop — 打断当前查询\n"
                    "• /git — 显示 Git 状态\n"
                    "• /model — 查看模型配置\n"
                    "• /codex — 查看或配置 Codex MCP 状态\n"
                    "• /switch <路径> — 切换到另一个项目的 SuperCC 实例\n"
                    "• /restart — 重启当前 SuperCC\n"
                    "• /update — 检查并更新到最新版本\n"
                    "• /help — 显示本帮助\n"
                    "• /memory — 查看/管理记忆\n"
                    "• /skill [all] — 查看技能列表"
                ),
            )
        elif cmd == "/git":
            return await self._handle_git(message)
        elif cmd == "/model":
            return await self._handle_model(message, arg)
        elif cmd == "/codex":
            return await self._handle_codex(message, arg)
        elif cmd == "/switch":
            return await self._handle_switch(message)
        elif cmd == "/restart":
            return await self._handle_restart(message)
        elif cmd == "/update":
            return await self._handle_update(message)
        elif cmd == "/memory":
            return await self._handle_memory(message)
        elif cmd == "/skill":
            return await self._handle_skill(message)
        else:
            return HandlerResult(success=True, response_text=f"未知命令: {cmd}")

    async def _handle_status(self, message: WeComIncomingMessage) -> HandlerResult:
        import os
        from supercc import __version__
        session = self.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="wecom")
        if not session:
            return HandlerResult(success=True, response_text="暂无活跃会话")
        try:
            from supercc.claude.model_config import get_model_env
            from supercc.claude.model_providers import PROVIDERS
            env = get_model_env()
            pid = env.provider_id
            mid = env.ANTHROPIC_MODEL
            provider = PROVIDERS.get(pid)
            model_provider = provider.id if provider else (pid or "未设置")
            model_id = mid or "未设置"
        except Exception:
            model_provider = "未知"
            model_id = "未知"
        try:
            import subprocess
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                text=True, cwd=session.project_path,
            ).strip()
            if not branch:
                branch = "(无分支)"
        except Exception:
            branch = "无分支"
        title = f"🐲龙王 **SuperCC v{__version__}**"
        try:
            from supercc.restarter import check_version
            current_ver, latest_ver = await asyncio.to_thread(check_version)
            def _ver_gt(current: str, latest: str) -> bool:
                import re
                def nums(v):
                    return [int(x) for x in re.findall(r'\d+', v)]
                return nums(latest) > nums(current)
            if _ver_gt(current_ver, latest_ver):
                title = f"🐲龙王 **SuperCC v{__version__} — 🌟可更新 v{latest_ver}🌟**"
        except Exception:
            pass
        sdk_sid = session.sdk_session_id or "(未建立)"
        def _count_skills(skills_dir: str) -> int:
            try:
                from pathlib import Path
                p = Path(skills_dir)
                if not p.exists():
                    return 0
                return sum(1 for item in p.iterdir() if item.is_dir() and (item / "SKILL.md").exists())
            except Exception:
                return 0
        project_skills = _count_skills(os.path.join(session.project_path, ".supercc", "skills"))
        global_skills = _count_skills(os.path.expanduser("~/.claude/skills"))
        text = (
            f"{title}\n\n"
            f"| 项目 | 值 |\n"
            f"|------|----|\n"
            f"| 进程ID | `{os.getpid()}` |\n"
            f"| 会话ID | `{sdk_sid}` |\n"
            f"| 消息数 | {session.message_count} |\n"
            f"| 累计费用 | `${session.total_cost:.4f}` |\n"
            f"| 供应商 | {model_provider} |\n"
            f"| 模型ID | `{model_id}` |\n"
            f"| Git分支 | `{branch}` |\n"
            f"| 工作目录 | `{session.project_path}` |\n"
            f"| 项目技能数 | {project_skills} |\n"
            f"| 全局技能数 | {global_skills} |"
        )
        return HandlerResult(success=True, response_text=text)

    async def _handle_stop(self, message: WeComIncomingMessage) -> HandlerResult:
        async with self._workers_lock:
            worker = self._session_workers.get(message.chat_id)
        if worker is None or not worker._running:
            return HandlerResult(success=True, response_text="当前没有正在运行的查询。")
        worker.claude.stop_event.set()
        if worker.task is not None and not worker.task.done():
            worker.task.cancel()
            worker.task = None
        return HandlerResult(success=True, response_text="🛑 已打断 Claude，当前任务已停止。")

    async def _handle_git(self, message: WeComIncomingMessage) -> HandlerResult:
        import subprocess
        def run_git(args: list[str]) -> str:
            try:
                result = subprocess.run(
                    ["git"] + args, capture_output=True, text=True, timeout=10,
                    cwd=self.approved_directory
                )
                return result.stdout.strip()
            except Exception:
                return ""
        branch = run_git(["branch", "--show-current"])
        if not branch:
            branch = "(无分支)"
        status_output = run_git(["status", "--porcelain"])
        log_lines = run_git(["log", "--format=%cI %h %s", "-5"]).splitlines()
        lines = [f"🌟 **Git Status - {branch}**", "", "📝 **变更文件**"]
        if status_output:
            for line in status_output.splitlines():
                lines.append(f"  {line}")
        else:
            lines.append("✅ 工作区干净，无待提交变更")
        lines.extend(["", "📋 **最近提交**", "", "| 时间 | Hash | 描述 |", "|------|------|------|"])
        for log_line in log_lines:
            parts = log_line.split(" ", 2)
            if len(parts) >= 3:
                dt_clean = parts[0].replace("T", " ")[:16]
                h = parts[1]
                msg = parts[2]
                lines.append(f"| {dt_clean} | `{h}` | {msg} |")
        return HandlerResult(success=True, response_text="\n".join(lines))

    async def _handle_model(self, message: WeComIncomingMessage, subcmd: str = "") -> HandlerResult:
        from supercc.claude.model_config import get_all_providers, get_model_env, set_project_model
        from supercc.claude.model_providers import PROVIDERS
        env = get_model_env()
        if subcmd:
            parts = subcmd.strip().split(maxsplit=2)
            action = parts[0].lower()
            target_pid = parts[1] if len(parts) > 1 else ""
            target_model = parts[2] if len(parts) > 2 else ""
            if action == "switch":
                if not target_pid:
                    return HandlerResult(success=True, response_text="请指定 provider ID")
                provider = PROVIDERS.get(target_pid)
                if not provider:
                    return HandlerResult(success=True, response_text=f"未知 provider `{target_pid}`")
                if not target_model:
                    return HandlerResult(success=True, response_text=f"请指定模型 ID。可用：{' / '.join(provider.models)}")
                if target_model not in provider.models:
                    return HandlerResult(success=True, response_text=f"模型 `{target_model}` 不在可用列表中")
                ok, err = set_project_model(self.data_dir, target_pid, target_model)
                if not ok:
                    return HandlerResult(success=True, response_text=f"切换失败：{err}")
                return HandlerResult(success=True, response_text=f"✅ 已切换为 `{provider.id}`（模型：`{target_model}`）")
        current_mid = env.ANTHROPIC_MODEL
        current_pid = env.provider_id
        providers_cfg = get_all_providers()
        lines = ["## 🤖 模型配置", f"当前使用：**{current_pid or '未设置'}**（`{current_mid or '未设置'}`）", ""]
        for pid, provider in PROVIDERS.items():
            if pid == "custom":
                continue
            pcfg = providers_cfg.get(pid)
            api_key = pcfg.api_key if pcfg else ""
            mark = "✅" if pid == current_pid else " "
            lines.append(f"{mark} `{pid}`: {api_key[:6] + '***' + api_key[-4:] if len(api_key) > 10 else '未配置'}")
            lines.append(f"   可用模型：{' / '.join(f'`{m}`' for m in provider.models)}")
        lines.append("\n💡 切换：`/model switch <provider_id> <model_id>`")
        return HandlerResult(success=True, response_text="\n".join(lines))

    async def _handle_codex(self, message: WeComIncomingMessage, arg: str = "") -> HandlerResult:
        parts = (arg or "").strip().split()
        first_word = parts[0].lower() if parts else "status"
        if first_word == "status":
            status = get_codex_mcp_status(self.config.codex)
            return HandlerResult(success=True, response_text=format_codex_status(status))
        if first_word in {"available", "availability", "ready"}:
            status = get_codex_mcp_status(self.config.codex)
            return HandlerResult(success=True, response_text=format_codex_availability(status))
        if first_word in {"models", "model"}:
            return HandlerResult(success=True, response_text=format_codex_models())
        if first_word == "setup":
            status = ensure_codex_mcp_configured(self.config.codex)
            return HandlerResult(success=True, response_text=format_codex_status(status))
        return HandlerResult(
            success=True,
            response_text=(
                "Codex 命令：\n"
                "• /codex status — 查看 Codex MCP 状态\n"
                "• /codex available — 快速判断 Codex 当前是否可用\n"
                "• /codex models — 查看 Codex 可选模型\n"
                "• /codex setup — 立即写入/刷新 Claude Code 的 Codex MCP 配置"
            ),
        )

    async def _handle_switch(self, message: WeComIncomingMessage) -> HandlerResult:
        from supercc.switcher import run_switch, SwitchError as SwitchErr
        parts = message.content.split(maxsplit=1)
        if len(parts) < 2:
            return HandlerResult(success=True, response_text="用法: /switch <目标项目路径>")
        raw_path = parts[1].strip()
        target = os.path.expanduser(raw_path) if raw_path.startswith("/") or raw_path.startswith("~") else os.path.abspath(raw_path)
        try:
            await run_switch(target, self.wecom, message.chat_id, message.message_id)
        except SwitchErr as e:
            return HandlerResult(success=True, response_text=f"❌ 切换失败：{e}")
        return HandlerResult(success=True)

    async def _handle_restart(self, message: WeComIncomingMessage) -> HandlerResult:
        from supercc.restarter import run_restart
        from supercc.main import _active_lock
        async for step in run_restart(_active_lock, self.wecom, message.chat_id, message.message_id):
            if step.status == "final":
                os._exit(0)
        return HandlerResult(success=True)

    async def _handle_update(self, message: WeComIncomingMessage) -> HandlerResult:
        from supercc.restarter import run_update
        from supercc.main import _active_lock
        did_update = await run_update(_active_lock, self.wecom, message.chat_id, message.message_id)
        if did_update:
            os._exit(0)
        return HandlerResult(success=True)

    async def _handle_memory(self, message: WeComIncomingMessage) -> HandlerResult:
        parts = message.content.split(maxsplit=3)
        scope = parts[1].lower() if len(parts) > 1 else ""
        action = parts[2].lower() if len(parts) > 2 else ""
        raw_args = parts[3].strip() if len(parts) > 3 else ""
        if not scope:
            return HandlerResult(success=True, response_text=self._memory_help())
        if scope == "user":
            return await self._handle_memory_user(message.user_open_id, action, raw_args)
        if scope == "proj":
            return await self._handle_memory_proj(message, action, raw_args)
        return HandlerResult(success=True, response_text=f"未知 scope: {scope}")

    def _memory_help(self) -> str:
        return "\n".join([
            "【记忆系统指令】\n",
            "/memory user add <title>|<content>|<keywords> — 新增用户偏好",
            "/memory user del <id> — 删除用户偏好",
            "/memory user update <id> <title>|<content>|<keywords> — 编辑用户偏好",
            "/memory user list — 列出用户偏好",
            "/memory user search <关键词> — 搜索用户偏好",
            "",
            "/memory proj add <title>|<content>|<keywords> — 新增项目记忆",
            "/memory proj del <id> — 删除项目记忆",
            "/memory proj update <id> <title>|<content>|<keywords> — 编辑项目记忆",
            "/memory proj list — 列出项目记忆",
            "/memory proj search <关键词> — 搜索项目记忆",
        ])

    async def _handle_memory_user(self, user_open_id: str, action: str, raw_args: str) -> HandlerResult:
        platform = get_current_platform()
        if action == "add":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True, response_text="用法: /memory user add <title>|<content>|<keywords>")
            p = self.memory_manager.add_preference(user_open_id, parts[0].strip(), parts[1].strip(), parts[2].strip(), platform=platform)
            return HandlerResult(success=True, response_text=f"✅ 用户偏好已保存（ID: {p.id}）")
        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user del <id>")
            ok = self.memory_manager.delete_preference(raw_args, user_open_id=user_open_id, platform=platform)
            return HandlerResult(success=True, response_text=f"用户偏好 {raw_args} {'已删除' if ok else '未找到'}")
        elif action == "update":
            parts = raw_args.split("|", 2)
            if len(parts) < 3:
                return HandlerResult(success=True, response_text="用法: /memory user update <id> <title>|<content>|<keywords>")
            ok = self.memory_manager.update_preference(parts[0].strip(), parts[1].strip(), parts[2].strip(), "", user_open_id=user_open_id, platform=platform)
            return HandlerResult(success=True, response_text=f"用户偏好 {parts[0].strip()} {'已更新' if ok else '未找到'}")
        elif action == "list":
            prefs = self.memory_manager.get_preferences_by_user(user_open_id, platform=platform)
            if not prefs:
                return HandlerResult(success=True, response_text="📭 暂无用户偏好记录")
            return HandlerResult(success=True, response_text=self._fmt_pref_table(prefs, len(prefs)))
        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user search <关键词>")
            results = self.memory_manager.search_preferences(raw_args, user_open_id=user_open_id, platform=platform)
            if not results:
                return HandlerResult(success=True, response_text=f"未找到与「{raw_args}」相关的用户偏好")
            return HandlerResult(success=True, response_text=self._fmt_pref_table(results, len(results)))
        else:
            return HandlerResult(success=True, response_text=f"未知 user action: {action}")

    async def _handle_memory_proj(self, message: WeComIncomingMessage, action: str, raw_args: str) -> HandlerResult:
        platform = get_current_platform()
        chat_id = message.chat_id or ""
        if action == "add":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True, response_text="用法: /memory proj add <title>|<content>|<keywords>")
            m = self.memory_manager.add_project_memory(
                self.approved_directory, parts[0].strip(), parts[1].strip(), parts[2].strip(),
                platform=platform, chat_id=chat_id,
            )
            return HandlerResult(success=True, response_text=f"✅ 项目记忆已保存（ID: {m.id}）")
        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj del <id>")
            ok = self.memory_manager.delete_project_memory(raw_args, self.approved_directory, platform=platform, chat_id=chat_id)
            return HandlerResult(success=True, response_text=f"项目记忆 {raw_args} {'已删除' if ok else '未找到'}")
        elif action == "update":
            parts = raw_args.split("|", 2)
            if len(parts) < 3:
                return HandlerResult(success=True, response_text="用法: /memory proj update <id> <title>|<content>|<keywords>")
            ok = self.memory_manager.update_project_memory(parts[0].strip(), parts[1].strip(), parts[2].strip(), "", self.approved_directory, platform=platform, chat_id=chat_id)
            return HandlerResult(success=True, response_text=f"项目记忆 {parts[0].strip()} {'已更新' if ok else '未找到'}")
        elif action == "list":
            mems = self.memory_manager.get_project_memories(self.approved_directory, platform=platform, chat_id=chat_id)
            if not mems:
                return HandlerResult(success=True, response_text="📭 暂无项目记忆记录")
            return HandlerResult(success=True, response_text=self._fmt_proj_table(mems, len(mems)))
        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj search <关键词>")
            results = self.memory_manager.search_project_memories(raw_args, self.approved_directory, platform=platform, chat_id=chat_id)
            if not results:
                return HandlerResult(success=True, response_text=f"未找到与「{raw_args}」相关的项目记忆")
            mems = [r.memory for r in results]
            return HandlerResult(success=True, response_text=self._fmt_proj_table(mems, len(mems)))
        else:
            return HandlerResult(success=True, response_text=f"未知 proj action: {action}")

    def _fmt_pref_table(self, prefs: list, total: int) -> str:
        header = f"👤 **用户偏好**（共 {total} 条）\n\n| # | 标题 | 内容摘要 | 关键词 | ID |\n|---|------|----------|--------|---|"
        for i, p in enumerate(prefs, start=1):
            content_short = p.content[:60] + ("…" if len(p.content) > 60 else "")
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
            header += f"\n| {i} | {esc(p.title)} | {esc(content_short)} | {esc(p.keywords)} | `{p.id}` |"
        return header

    def _fmt_proj_table(self, mems: list, total: int) -> str:
        header = f"📁 **项目记忆**（共 {total} 条）\n\n| # | 标题 | 内容摘要 | 关键词 | ID |\n|---|------|----------|--------|---|"
        for i, m in enumerate(mems, start=1):
            content_short = m.content[:60] + ("…" if len(m.content) > 60 else "")
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
            header += f"\n| {i} | {esc(m.title)} | {esc(content_short)} | {esc(m.keywords)} | `{m.id}` |"
        return header

    async def _handle_skill(self, message: WeComIncomingMessage) -> HandlerResult:
        parts = message.content.split(maxsplit=1)
        scope_all = len(parts) > 1 and parts[1].strip().lower() == "all"
        if scope_all:
            skills_dir = Path.home() / ".claude" / "skills"
            title = "全局 Skills"
        else:
            project_path = getattr(self, "_current_project_path", "") or self.approved_directory
            skills_dir = Path(project_path) / ".supercc" / "skills"
            title = f"项目 Skills（{Path(project_path).name}）"
        if not skills_dir.exists():
            return HandlerResult(success=True, response_text=f"📭 暂无 {'全局' if scope_all else '项目'} Skills")
        skill_entries = []
        for item in skills_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                name = item.name
                description = ""
                try:
                    content = (item / "SKILL.md").read_text(encoding="utf-8")
                    import re
                    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                    if m:
                        for line in m.group(1).splitlines():
                            if line.startswith("name:"):
                                name = line.split(":", 1)[1].strip()
                            elif line.startswith("description:"):
                                description = line.split(":", 1)[1].strip()
                                break
                except Exception:
                    pass
                skill_entries.append((item.name, name, description))
        if not skill_entries:
            return HandlerResult(success=True, response_text=f"📭 暂无 {'全局' if scope_all else '项目'} Skills")
        lines = [f"## 🛠 {title}（共 {len(skill_entries)} 个）\n", "| 目录名 | Skill 名称 | 描述 |", "|--------|-----------|------|"]
        for dirname, name, description in skill_entries:
            desc_short = description[:40] + "…" if len(description) > 40 else description
            lines.append(f"| `{dirname}` | {name} | {desc_short} |")
        return HandlerResult(success=True, response_text="\n".join(lines))

    async def _safe_send(self, chat_id: str, reply_to_message_id: str, text: str, log_reply: bool = True, preformatted: bool = False):
        try:
            formatted = text if preformatted else self.formatter.format_text(text)
            if not formatted.strip():
                return
            await self.wecom.send_markdown_reply(chat_id, formatted, reply_to_message_id)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")

    def _extract_quoted_content(self, message: dict) -> str:
        msg_type = message.get("msg_type", "")
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
            if msg_type == "text":
                return content.get("text", "")
            elif msg_type == "markdown":
                return content.get("content", "")
        except Exception:
            pass
        return str(content_str)

    async def _preprocess_media(self, message: WeComIncomingMessage) -> str:
        from supercc.adapter.wecom.media import make_image_path, make_file_path, save_bytes
        if message.message_type not in ("image", "file"):
            return ""
        msg_id = message.message_id
        logger.info(f"[media] preprocessing {message.message_type} message {msg_id}")
        msg_data = await self.wecom.get_message(msg_id)
        if not msg_data:
            logger.warning(f"[media] failed to fetch message {msg_id}")
            return ""
        content_str = msg_data.get("content", "{}")
        try:
            content = json.loads(content_str)
        except Exception:
            logger.warning(f"[media] json.loads failed on {content_str!r}")
            return ""
        data_dir = self.data_dir or os.getcwd()
        if message.message_type == "image":
            image_key = content.get("image_key") or content.get("url") or ""
            if not image_key:
                logger.warning(f"[media] no image key in message {msg_id}")
                return ""
            base_path = make_image_path(data_dir, msg_id)
            try:
                data = await self.wecom.download_media(msg_id, image_key, msg_type="image")
                save_path = base_path + ".png"
                save_bytes(save_path, data)
                return f"![image]({save_path})"
            except Exception as e:
                logger.warning(f"[media] failed to download image: {e}")
                return ""
        elif message.message_type == "file":
            file_key = content.get("file_key") or content.get("url") or ""
            orig_name = content.get("file_name", "file")
            file_type = content.get("file_type", "bin")
            if not file_key:
                logger.warning(f"[media] no file key in message {msg_id}")
                return ""
            save_path = make_file_path(data_dir, msg_id, orig_name, file_type)
            try:
                data = await self.wecom.download_media(msg_id, file_key, msg_type="file")
                save_bytes(save_path, data)
                return f"[File: {save_path}] ({orig_name})"
            except Exception as e:
                logger.warning(f"[media] failed to download file: {e}")
                return ""
        return ""


def _is_codex_tool_name(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    return tool_name == "codex" or tool_name == "mcp__codex__codex"


def _format_codex_event(event: CodexStreamEvent) -> str:
    if event.type == "text":
        return event.content
    elif event.type == "command_execution":
        cmd = (event.command or "").strip()
        content = (event.content or "").strip()
        parts = []
        if cmd:
            parts.append(f"```bash\n{cmd}\n```")
        if content:
            parts.append(content)
        return "\n\n".join(parts)
    elif event.type == "command_output":
        return ""
    elif event.type == "tool_use":
        tool = event.tool_name or "unknown"
        inp = event.tool_input or ""
        if len(inp) > 800:
            inp = inp[:800] + "..."
        return f"tool: `{tool}`\n\n```json\n{inp}\n```"
    elif event.type == "file_change":
        path = event.file_path or ""
        return f"`{path}`\n\n{event.content[:300]}"
    elif event.type == "reasoning":
        return event.content[:500]
    elif event.type == "todo_list":
        return event.content[:1000]
    elif event.type == "error":
        return event.content[:500]
    elif event.type == "finished":
        ec = event.exit_code
        if ec is None or ec == 0:
            return event.content or "执行完成"
        return f"{event.content}\n\nexit_code: `{ec}`".strip()
    elif event.type == "started":
        return event.content or "started"
    return ""

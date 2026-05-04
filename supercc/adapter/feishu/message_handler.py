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

from supercc.adapter.feishu.client import FeishuClient, IncomingMessage
from supercc.security.auth import Authenticator
from supercc.security.validator import SecurityValidator
from supercc.claude.integration import ClaudeIntegration
from supercc.claude.message_context import get_current_platform, set_current_context
from supercc.claude.memory_manager import get_memory_manager, MEMORY_SYSTEM_GUIDE
from supercc.claude.feishu_file_tools import FEISHU_FILE_GUIDE
from supercc.claude.cron_tools import CRON_GUIDE
from supercc.claude.codex_mcp import (
    ensure_codex_mcp_configured,
    format_codex_availability,
    format_codex_models,
    format_codex_status,
    get_codex_mcp_guide,
    get_codex_mcp_status,
)
from supercc.claude.codex_exec import CodexStreamEvent
from supercc.claude.session_manager import SessionManager
from supercc.evolve.skill_nudge import SkillNudge, trigger_skill_review
from supercc.adapter.feishu.format.agent_card import format_agent_card, format_codex_card
from supercc.adapter.feishu.format.reply_formatter import ReplyFormatter
from supercc.adapter.feishu.format.edit_diff import _DiffMarker, _MemoryCardMarker
from supercc.adapter.feishu.format.questionnaire_card import _AskUserQuestionMarker, format_questionnaire_card

logger = logging.getLogger(__name__)


def _is_codex_tool_name(tool_name: str | None) -> bool:
    """Return True for Claude SDK's plain and MCP-qualified Codex tool names."""
    if not tool_name:
        return False
    return tool_name == "codex" or tool_name == "mcp__codex__codex"




def _format_codex_event(event: CodexStreamEvent) -> str:
    """Format a CodexStreamEvent for Feishu rendering."""
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

# Match a slash-command like "/stop", "/new", "/feishu auth", "/status foo"
# Commands: / + letter + word-chars, optionally followed by space + args
# NOT a path: paths contain slashes later (e.g. /Users/x/...)
_COMMAND_RE = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_-]*(?:\s.*)?$")

# Config file names that trigger auto-reload when uploaded via Feishu
_CONFIG_RELOAD_NAMES = {"config.yaml", "config.json"}


def _is_config_file_by_orig_name(orig_name: str) -> bool:
    """Return True if orig_name is a known config file name."""
    return orig_name.lower() in _CONFIG_RELOAD_NAMES


def _is_command(text: str) -> bool:
    """Return True if text looks like a slash command, not a Unix path."""
    return bool(_COMMAND_RE.match(text))


def _strip_mention_prefix(content: str) -> str:
    """Strip @_user_N prefix from message content if present.

    When a user sends '@_user_1 /git' in group chat, the content starts with
    the mention. This removes the mention so the underlying command is visible.
    """
    # Match @_user_N followed by optional whitespace
    return re.sub(r"^@_user_\d+\s*", "", content)


@dataclass
class HandlerResult:
    success: bool
    response_text: str | None = None
    error: str | None = None


class StreamAccumulator:
    """Accumulates streaming text chunks and flushes them to Feishu in batches.

    Feishu message updates are expensive (one message per API call), so we buffer
    chunks and flush when a tool call arrives or after a short idle period.
    Tracks `sent_something` so the caller knows whether to skip the final response.
    """

    def __init__(self, chat_id: str, message_id: str, send_fn, flush_timeout: float = 1.5):
        self.chat_id = chat_id
        self._message_id = message_id
        self._send = send_fn
        self._flush_timeout = flush_timeout
        self._buffer = ""
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None
        self.sent_something = False  # True once any text has been flushed

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
        """Send accumulated text to Feishu immediately."""
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


class SessionWorker:
    """单个 chat_id 的消息处理器（per-chat-id 并行核心）"""

    def __init__(self, chat_id: str, handler: "MessageHandler"):
        import time

        self.chat_id = chat_id
        self.queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
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
        self.IDLE_TIMEOUT = 300  # 5分钟
        self._current_group_members: list | None = None  # Worker 私有，避免竞态
        self._new_session_requested = False  # /new 标志，SessionWorker 私有
        self._seen_chat_ids: set = set()  # 记录该 Worker 已处理过的 chat_id，避免每次都创建新 session
        # 当前消息上下文，供工具函数（_get_user_open_id 等）使用
        self._current_user_open_id: str | None = None
        self._current_chat_id: str | None = None
        self._current_platform: str = "feishu"

    def _trigger_memory_review(self, message: IncomingMessage, response_text: str) -> None:
        """Worker 私有：使用自己的 claude_memory 实例触发记忆回顾"""
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
                        card = h._render_memory_card(result)
                        try:
                            await h.feishu.send_card(message.chat_id, card)
                        except Exception:
                            await h._safe_send(message.chat_id, message.message_id, str(card))
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
                    self.queue.task_done()  # 确保即使异常也调用
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(f"Worker {self.chat_id} error")

        self._running = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Worker 退出时清理"""
        pass  # ClaudeIntegration 无需显式清理

    def update_chat_id(self, chat_id: str) -> None:
        """复用时更新 chat_id"""
        self.chat_id = chat_id
        # 共享 approved_directory，session 隔离由数据库层 + SDK continue_conversation 控制

        # 重置 options，下一次 query 会用新的 approved_directory 重建
        self.claude.mark_system_prompt_stale()
        self.claude_memory.mark_system_prompt_stale()
        self.claude_skill.mark_system_prompt_stale()

    async def _process_message(self, message: IncomingMessage) -> None:
        """处理单条消息：鉴权 → 媒体预处理 → 引用检测 → 查询"""
        h = self.handler

        # 设置当前消息上下文，供工具函数通过 message_context.py 获取
        self._current_user_open_id = message.user_open_id
        self._current_chat_id = message.chat_id
        self._current_platform = "feishu"
        set_current_context(message.user_open_id, message.chat_id, "feishu")

        # P2P: allowed_users whitelist applies. Group @mention: controlled by GroupConfigEntry.
        if not message.is_group_chat:
            auth_result = h.auth.authenticate(message.user_open_id)
            if not auth_result.authorized:
                logger.info(f"Ignoring message from unauthorized user: {message.user_open_id}")
                return

        # Group chat: skip if bot was not @mentioned (no response to avoid spam)
        # Group access control check (per-group config: enabled, allow_from, require_mention)
        if not await h._check_group_access(message):
            return

        if message.message_type not in ("text", "image", "file"):
            await h._safe_send(message.chat_id, message.message_id, "暂不支持该消息类型，请发送文字消息。")
            return

        # For group chat, use chat-specific session lookup to isolate group sessions
        # from p2p sessions. For p2p, use the standard user-level session.
        if message.is_group_chat:
            session = h.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="feishu")
            if session is None:
                # First message in this group chat — create a new session
                session = h.sessions.create_session(
                    message.user_open_id,
                    h.approved_directory,
                    chat_id=message.chat_id,
                    platform="feishu",
                )
            elif session.chat_id != message.chat_id:
                # Same user in a different group — update session to point to new chat
                h.sessions.update_chat_id(message.user_open_id, message.chat_id, platform="feishu")
        else:
            # P2P: use chat-specific session lookup to avoid cross-contamination with group sessions
            session = h.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="feishu")
            if session is None:
                session = h.sessions.create_session(
                    message.user_open_id,
                    h.approved_directory,
                    chat_id=message.chat_id,
                    platform="feishu",
                )

        project_path = session.project_path if session else h.approved_directory
        h._current_project_path = project_path  # 供 stream_callback 使用

        # 读取 AGENTS.md（如存在）注入到 system prompt 最前面
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
            + FEISHU_FILE_GUIDE
            + CRON_GUIDE
            + get_codex_mcp_guide(h.config.codex, codex_status)
            + h.memory_manager.inject_context(
                user_open_id=message.user_open_id,
                project_path=project_path,
                platform=get_current_platform(),
                chat_id=message.chat_id,
            )
        )

        # 群聊时：获取成员列表，注入 @mention 指令到 system prompt
        if message.is_group_chat and message.chat_id:
            # 每次消息都检查权限（在所有群聊接口调用之前执行）
            already_sent_key = f"_perm_sent_{message.chat_id}"
            try:
                perm = await h.feishu.check_group_permissions(message.chat_id)
                auth_url = perm.get("auth_url", "")
                missing = []
                if not perm.get("history_ok"):
                    missing.append("读取群聊历史（im:message.group_msg）")
                if not perm.get("members_ok"):
                    missing.append("读取群成员信息（im:chat.members:read）")
                if missing:
                    logger.warning(f"[GROUP_PERM] missing permissions in {message.chat_id}: {missing}")
                    # 首次权限不足：发送授权卡片，然后 return
                    if not getattr(h, already_sent_key, False):
                        setattr(h, already_sent_key, True)
                        missing_text = "\n".join(f"- {m}" for m in missing)
                        card = {
                            "schema": "2.0",
                            "body": {
                                "elements": [
                                    {"tag": "markdown", "content": f"## ⚠️ 权限不足，无法正常服务\n\n当前机器人缺少以下权限：\n\n{missing_text}\n\n请管理员前往飞书开放平台授权：\n{auth_url}\n\n授权完成后再重新发送消息。"},
                                ]
                            }
                        }
                        try:
                            await h.feishu.send_interactive(message.chat_id, card, message.message_id)
                        except Exception as card_err:
                            err_str = str(card_err)
                            fallback = (
                                f"⚠️ 授权卡片发送失败\n\n"
                                f"Feishu 错误：{err_str}\n\n"
                                f"缺少权限：\n{missing_text}\n\n"
                                f"授权链接：{auth_url}"
                            )
                            await h._safe_send(message.chat_id, message.message_id, fallback)
                            logger.warning(f"[GROUP_PERM] card send failed, fallback text sent: {card_err}")
                        return  # 首次权限不足：return，等用户重新发
                    # 已发过授权卡片：继续正常处理（功能受限但不阻塞）
            except Exception as ex:
                logger.warning(f"[GROUP_PERM] permission check failed: {ex}")
            try:
                members = await h.feishu.get_chat_members(message.chat_id)
                self._current_group_members = members  # Worker 私有，避免竞态
                if members:
                    # 查找发送者名称
                    sender_name = None
                    for m in members:
                        if isinstance(m, dict):
                            member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                        else:
                            member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or ""
                        if member_id == message.user_open_id:
                            sender_name = m.get("name") or m.get("bot_name") or getattr(m, "name", None) or ""
                            break
                    lines = [
                        f"【群聊规则】必须在最终回复里艾特@{sender_name or message.user_open_id} 以及相关人员。需要使用飞书特定的@格式如：{{消息内容}}<at user_id=\"open_id\">姓名</at>。不得遗漏。",
                    ]
                    for m in members:
                        if isinstance(m, dict):
                            member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                            name = m.get("name") or m.get("bot_name", "")
                        else:
                            member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or ""
                            name = getattr(m, "name", None) or ""
                        if member_id and name:
                            lines.append(f"  {name}: <at user_id=\"{member_id}\">{name}</at>")
                    system_prompt_append += "\n".join(lines) + "\n"
                    # 持久化 group_members 到 session（供 _is_group_chat 判断用）
                    try:
                        def _member_to_dict(m):
                            if isinstance(m, dict):
                                return {
                                    "id": m.get("member_id") or m.get("open_id") or m.get("bot_id", ""),
                                    "name": m.get("name") or m.get("bot_name", ""),
                                }
                            return {
                                "id": getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", ""),
                                "name": getattr(m, "name", None) or "",
                            }
                        members_json = json.dumps([d for d in (_member_to_dict(m) for m in members) if d["id"]], ensure_ascii=False)
                        h.sessions.update_group_members(session.session_id, members_json)
                    except Exception as e:
                        logger.debug(f"[GROUP_MEMBERS] failed to persist: {e}")
            except Exception as ex:
                logger.warning(f"[GROUP_MENTION] failed to get members: {ex}")

        # 确保 options 已初始化，首次对该 chat_id 发消息时创建新 session
        self._init_options(system_prompt_append, _init_chat_id=message.chat_id)

        await self._run_query(message, session)

    def _init_options(
        self,
        system_prompt_append: str | None = None,
        continue_conversation: bool = True,
        _init_chat_id: str | None = None,
    ) -> None:
        """初始化/更新持久化 options"""
        # /new 设置了 _new_session_requested，下次 query 用 continue_conversation=False
        if self._new_session_requested:
            self._new_session_requested = False
            continue_conversation = False
        # 首次对该 chat_id 发消息时用 False（新 session），后续用 True
        elif _init_chat_id and _init_chat_id not in self._seen_chat_ids:
            self._seen_chat_ids.add(_init_chat_id)
            continue_conversation = False
        self.claude._init_options(system_prompt_append, continue_conversation, channel="feishu")

    async def _run_query(
        self,
        message: IncomingMessage,
        session,
    ) -> None:
        """Run Claude query in background, send results to Feishu on completion."""
        h = self.handler
        reaction_id = None
        _last_response = ""

        async def _show_typing() -> None:
            """在 _query_lock 拿到后显示 typing（通过 on_start 回调传入 query）"""
            nonlocal reaction_id
            reaction_id = await h.feishu.add_typing_reaction(message.message_id)
            logger.info(f"[typing] on — user={message.user_open_id}, reaction_id={reaction_id!r}")

        try:
            # Audio is not yet supported — tell the user and skip Claude
            if message.message_type == "audio":
                await h._safe_send(message.chat_id, message.message_id, "🎙️ 暂不支持语音消息，请发送文字消息。")
                return

            # Preprocess media (image/file) before querying Claude
            media_prompt_prefix = ""
            media_notify_text = ""
            logger.debug(f"[_run_query] message_type={message.message_type!r}")
            if message.message_type in ("image", "file"):
                logger.debug(f"[_run_query] entering media branch for {message.message_type}")
                try:
                    media_prompt_prefix = await h._preprocess_media(message)
                    if media_prompt_prefix:
                        logger.info(f"Inbound media saved: {media_prompt_prefix}")

                        # Extract orig_name from return string: "[File: /path] (orig_name)"
                        orig_name = ""
                        _m = re.search(r"\]\s*\(([^)]+)\)\s*$", media_prompt_prefix)
                        if _m:
                            orig_name = _m.group(1)

                        # Check if this is a config file upload — trigger auto-reload
                        if orig_name and _is_config_file_by_orig_name(orig_name):
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
                            # Notify user in Feishu that media was received (only for non-config files)
                            icon = {"image": "🖼️", "file": "🗃"}.get(message.message_type, "🗃")
                            media_notify_text = f"{icon} 收到 {message.message_type}，正在分析..."
                            await h._safe_send(message.chat_id, message.message_id, media_notify_text)
                except Exception as e:
                    logger.warning(f"Failed to process inbound media: {e}")
                    media_prompt_prefix = ""

            # Resolve quoted message content
            quoted_content = ""
            if message.parent_id:
                try:
                    quoted_msg = await h.feishu.get_message(message.parent_id)
                    if quoted_msg:
                        sender_id = quoted_msg.get("sender_id", "")
                        quoted_text = h._extract_quoted_content(quoted_msg)
                        # Skip only if the user is quoting their OWN message (to avoid
                        # a user quoting themselves → bot sees it → bot replies → user
                        # quoting bot → loop). We do want to pass along quoted bot
                        # messages so the user can get contextual responses.
                        if sender_id == message.user_open_id:
                            quoted_content = ""  # User quoting themselves — skip
                        else:
                            quoted_content = f"[引用消息: {message.parent_id}] {quoted_text}"
                        logger.info(f"Quoted message {message.parent_id}: {quoted_text[:100]!r}")
                    else:
                        # get_message returned None — message not found/deleted
                        quoted_content = f"[引用消息不可用: {message.parent_id}]"
                        logger.warning(f"Quoted message {message.parent_id} not found")
                except Exception:
                    # Network/auth error — tell the user so they're not confused
                    quoted_content = f"[引用消息不可用: {message.parent_id}]"
                    logger.warning(f"Failed to fetch quoted message {message.parent_id}")

            # Inject group chat history so the bot has context of recent messages.
            # History was recorded for ALL group messages (including non-@mention ones).
            group_history_prefix = ""
            if message.is_group_chat and message.chat_id:
                hist = h._group_history.get(message.chat_id, [])
                if hist:
                    history_text = "\n".join(hist)
                    group_history_prefix = f"[群聊上下文]\n{history_text}\n\n"
                    logger.debug(f"[GROUP_HISTORY][INJECT] chat_id={message.chat_id} history_len={len(hist)} entries={hist!r}")
                else:
                    logger.debug(f"[GROUP_HISTORY][INJECT] chat_id={message.chat_id} NO_HISTORY (empty)")

            prefix_parts = [p for p in [group_history_prefix, media_prompt_prefix, quoted_content] if p]
            prefix = "\n".join(prefix_parts) + "\n" if prefix_parts else ""
            # For text messages: prepend prefix to actual text content.
            # For media messages (image/file): message.content may contain user text
            # (mixed image+text case). Use media prefix + user text.
            is_media = message.message_type in ("image", "file")
            if is_media and media_prompt_prefix:
                # Media messages: prepend prefix to any user text
                user_text = message.content.strip()
                if user_text:
                    full_prompt = (prefix + user_text).strip()
                else:
                    full_prompt = prefix.strip()
            else:
                # Text messages: prepend prefix to actual text content
                full_prompt = (prefix + message.content).strip()

            # Retry loop: SDK 有时会返回空结果（cost > 0 但无任何内容），
            # 常见于 /stop 后 CLI 状态不稳或 MCP server 临时故障。
            # 自动重试最多 3 次，每次用新的 accumulator 确保 stream 状态干净。
            last_cost = 0.0
            _stream_too_long = [False]
            # 预计算 mention tag（群聊时）
            mention_tag = ""
            if message.is_group_chat and self._current_group_members is not None:
                members = self._current_group_members
                for m in members:
                    if isinstance(m, dict):
                        member_id = m.get("member_id") or m.get("open_id") or ""
                        name = m.get("name") or ""
                    else:
                        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or ""
                        name = getattr(m, "name", None) or ""
                    if member_id == message.user_open_id and name:
                        mention_tag = f"\n<at user_id=\"{member_id}\">{name}</at>"
                        break
            for retry_round in range(3):
                accumulator = StreamAccumulator(message.chat_id, message.message_id, h._safe_send)

                async def stream_callback(stream_item):
                    # Handle Codex internal events — flush Claude accumulator and render Codex event
                    if isinstance(stream_item, CodexStreamEvent):
                        await accumulator.flush()
                        text = _format_codex_event(stream_item)
                        if text:
                            card = format_codex_card(
                                stream_item.type,
                                text,
                                {
                                    "command": stream_item.command,
                                    "exit_code": stream_item.exit_code,
                                    "tool_name": stream_item.tool_name,
                                },
                            )
                            try:
                                await h.feishu.send_interactive(message.chat_id, card, message.message_id)
                            except Exception:
                                await h._safe_send(message.chat_id, message.message_id, text, log_reply=False)
                        return

                    claude_msg = stream_item
                    # Codex MCP 工具调用 → 渲染成与 Agent card 一致的卡片
                    if _is_codex_tool_name(claude_msg.tool_name):
                        await accumulator.flush()
                        card = format_agent_card(claude_msg.tool_input or "", title="## 🤖 Codex")
                        try:
                            await h.feishu.send_interactive(message.chat_id, card, message.message_id)
                        except Exception:
                            await h._safe_send(message.chat_id, message.message_id, claude_msg.tool_input or "", log_reply=False)
                        return

                    if claude_msg.tool_name:
                        await accumulator.flush()
                        # 记忆工具传入 memory_manager 和默认 project_path
                        kwargs = {}
                        if claude_msg.tool_name.startswith("mcp__SuperCC__Memory"):
                            kwargs["memory_manager"] = h.memory_manager
                            kwargs["default_project_path"] = getattr(h, "_current_project_path", "")
                            kwargs["platform"] = get_current_platform()
                            kwargs["chat_id"] = message.chat_id or ""
                        result = h.formatter.format_tool_call(
                            claude_msg.tool_name,
                            claude_msg.tool_input,
                            **kwargs,
                        )
                        logger.info(f"[stream] tool: {claude_msg.tool_name} | input: {claude_msg.tool_input}")

                        # Hermes-style skill nudge: count tool calls (trigger after query completes)
                        nudge = h._skill_nudge
                        if nudge:
                            nudge.config.current_user = message.user_open_id
                            nudge.increment()

                        # Agent 工具 → CardKit 卡片（检测 tool_name 包含 "agent" 且 tool_input 有 "prompt"）
                        if "agent" in claude_msg.tool_name.lower():
                            import json
                            try:
                                data = json.loads(claude_msg.tool_input) if claude_msg.tool_input else {}
                            except json.JSONDecodeError:
                                data = {}
                            if "prompt" in data:
                                card = format_agent_card(claude_msg.tool_input)
                                try:
                                    await h.feishu.send_card(message.chat_id, card)
                                except Exception:
                                    logger.warning(f"send_card failed for agent tool, falling back")
                                return

                        # Plan 工具 → CardKit 卡片（📋 标题）
                        if claude_msg.tool_name in ("EnterPlanMode", "ExitPlanMode"):
                            title = "## 📋 Plan" if claude_msg.tool_name == "EnterPlanMode" else "## 📋 Plan End"
                            card = format_agent_card(claude_msg.tool_input or "", title=title)
                            try:
                                await h.feishu.send_card(message.chat_id, card)
                            except Exception:
                                logger.warning(f"send_card failed for plan tool, falling back")
                            return

                        # _DiffMarker / list[_DiffMarker] → 彩色卡片；其他 → backtick 格式
                        if isinstance(result, _DiffMarker):
                            for card in result.card if isinstance(result.card, list) else [result.card]:
                                try:
                                    await h.feishu.send_edit_diff_card(
                                        message.chat_id, card, message.message_id, log_reply=False
                                    )
                                except Exception:
                                    # 卡片发送失败，降级为带图标的纯文本
                                    import json
                                    try:
                                        data = json.loads(result.tool_input)
                                        file_path = data.get("file_path", "unknown")
                                        # 图标规则：Edit→✏️，cc-工具名→🧰，Bash调用skill→🧰，其他→📝
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
                                    logger.warning(f"send_edit_diff_card failed, falling back to: {fallback}")
                                    await h._safe_send(message.chat_id, message.message_id, fallback, log_reply=False)
                        elif isinstance(result, list):
                            for marker in result:
                                if isinstance(marker, _DiffMarker):
                                    for card in marker.card if isinstance(marker.card, list) else [marker.card]:
                                        try:
                                            await h.feishu.send_edit_diff_card(
                                                message.chat_id, card, message.message_id, log_reply=False
                                            )
                                        except Exception:
                                            import json
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
                                            logger.warning(f"send_edit_diff_card failed, falling back to: {fallback}")
                                            await h._safe_send(message.chat_id, message.message_id, fallback, log_reply=False)
                        elif isinstance(result, _MemoryCardMarker):
                            # 记忆工具 → CardKit 原生格式（绕过 markdown 渲染）
                            card = h._render_memory_card(result)
                            try:
                                await h.feishu.send_card(message.chat_id, card)
                            except Exception:
                                logger.warning(f"send_card failed for memory tool, falling back to text")
                                await h._safe_send(message.chat_id, message.message_id, str(card), log_reply=False)
                        elif isinstance(result, _AskUserQuestionMarker):
                            # AskUserQuestion → 精美飞书问卷卡片
                            if result.data is not None:
                                card = format_questionnaire_card(result)
                                try:
                                    await h.feishu.send_edit_diff_card(
                                        message.chat_id, card, message.message_id, log_reply=False
                                    )
                                except Exception as e:
                                    logger.warning(f"send_edit_diff_card failed for AskUserQuestion: {e}, falling back")
                                    await h._safe_send(
                                        message.chat_id, message.message_id,
                                        f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                        log_reply=False,
                                    )
                            else:
                                await h._safe_send(
                                    message.chat_id, message.message_id,
                                    f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                    log_reply=False,
                                )
                        else:
                            await h._safe_send(message.chat_id, message.message_id, result, log_reply=False)
                    elif claude_msg.content:
                        logger.info(f"[stream] text: {claude_msg.content[:100]}")
                        await accumulator.add_text(claude_msg.content)
                        _content_lower = claude_msg.content.lower()
                        if (
                            "too long" in _content_lower
                            or "超出" in _content_lower
                            or "context window" in _content_lower
                            or "context_length" in _content_lower
                            or "max_tokens" in _content_lower
                        ):
                            _stream_too_long[0] = True

                response, sdk_session_id_from_query, cost = await self.claude.query(
                    prompt=full_prompt,
                    on_stream=stream_callback,
                    on_start=_show_typing,
                )
                last_cost = cost

                # Flush any remaining buffered text
                await accumulator.flush()

                # 如果这次尝试有实质内容（发了任何消息或返回了文本），认为成功，退出重试循环
                if accumulator.sent_something or response:
                    _last_response = response or ""
                    if _stream_too_long[0]:
                        await h._safe_send(
                            message.chat_id, message.message_id,
                            "💡 上下文已满，发送 **/new** 可开启新会话，我会记住之前的进度。",
                            log_reply=False,
                        )
                    break

                # 这次尝试是空结果（cost > 0 但没有任何内容），重试
                if retry_round < 2:
                    logger.warning(
                        f"[_run_query] Empty response (cost={cost}), retrying "
                        f"({retry_round + 1}/3)"
                    )
            else:
                # 3 次重试全部失败
                logger.error(f"[_run_query] 3 次重试均失败，放弃查询")
                await h._safe_send(
                    message.chat_id, message.message_id,
                    "⚠️ 查询失败：SDK 返回空响应，请稍后重试。"
                )
                return

            # Save session
            if not session:
                session = h.sessions.create_session(
                    message.user_open_id,
                    h.approved_directory,
                    sdk_session_id=sdk_session_id_from_query,
                    chat_id=message.chat_id,
                    platform="feishu",
                )
            else:
                h.sessions.update_session(session.session_id, cost=last_cost, message_increment=1, update_last_message=True)

            # 存储 sdk_session_id（首次建立或变化时都更新；空值不覆盖有效值）
            new_sid = (sdk_session_id_from_query or "").strip()
            old_sid = (session.sdk_session_id or "").strip()
            if new_sid and new_sid != old_sid:
                logger.info(f"[_run_query] sdk_session_id: {old_sid!r} -> {new_sid!r}")
                h.sessions.update_sdk_session_id(session.session_id, new_sid)
                if old_sid:  # 旧值存在才通知（首次建无需通知）
                    await h._safe_send(
                        message.chat_id, message.message_id,
                        f"🔄 已切换到新 Session\nSession ID: `{new_sid}`",
                        log_reply=False,
                    )

            # 群聊时：检查回复是否已 mention 提问者本人，无则追加
            def _mentions_user(text: str, user_id: str) -> bool:
                return bool(text and f'<at user_id="{user_id}"' in text)

            if mention_tag:
                sender_id = message.user_open_id
                if not accumulator.sent_something and _last_response:
                    # 非流式：检查 response 是否已 mention 提问者
                    formatted = h.formatter.format_text(_last_response)
                    chunks = h.formatter.split_messages(formatted)
                    if chunks and not _mentions_user(chunks[-1], sender_id):
                        chunks[-1] = chunks[-1].rstrip() + mention_tag
                        for chunk in chunks:
                            await h._safe_send(message.chat_id, message.message_id, chunk, preformatted=True)
                else:
                    # 流式：检查 _buffer 是否已 mention 提问者，无则追加
                    async with accumulator._lock:
                        buffered = accumulator._buffer
                    if not _mentions_user(buffered, sender_id):
                        await h._safe_send(message.chat_id, message.message_id, mention_tag)

        except asyncio.CancelledError:
            await h._safe_send(message.chat_id, message.message_id, "🛑 已打断 Claude。")
        except Exception as e:
            logger.exception(f"Error in _run_query: {e}")
            # CLI 进程异常崩溃，每次 query 内部创建新 client，下一次自动恢复
            logger.warning(f"[_run_query] CLI error: {e}")
            error_msg = f"⚠️ 内部错误：{e}"
            await h._safe_send(message.chat_id, message.message_id, error_msg)
        finally:
            if reaction_id:
                logger.info(f"[typing] off — user={message.user_open_id}, reaction_id={reaction_id!r}")
                try:
                    await h.feishu.remove_typing_reaction(message.message_id, reaction_id)
                except Exception as exc:
                    logger.warning(f"[typing] remove_typing_reaction failed: {exc}")
            # Trigger memory review after [typing] off (Worker 私有实例)
            self._trigger_memory_review(message, _last_response)

            # Trigger skill nudge after query completes (not during streaming)
            nudge = h._skill_nudge
            if nudge and nudge._pending:
                logger.info("[_trigger_skill_review] starting background review")
                try:
                    if self.claude_skill._options is None:
                        self.claude_skill._init_options()
                    asyncio.create_task(
                        trigger_skill_review(
                            make_claude_query=lambda p: self.claude_skill.query(prompt=p),
                            nudge=nudge,
                            chat_id=message.chat_id,
                            send_to_feishu=lambda cid, text: h._safe_send(cid, message.message_id, text),
                            skills_dir=Path(h.data_dir) / "skills",
                        )
                    )
                except Exception as e:
                    logger.warning(f"[_trigger_skill_review] failed to start: {e}")


class MessageHandler:
    def __init__(
        self,
        feishu_client: FeishuClient,
        authenticator: Authenticator,
        validator: SecurityValidator,
        claude: ClaudeIntegration,
        session_manager: SessionManager,
        formatter: ReplyFormatter,
        approved_directory: str,
        config=None,
        data_dir: str = "",
        feishu_groups: dict | None = None,
        config_path: str | None = None,
        skill_nudge: SkillNudge | None = None,
    ):
        if config is None:
            from supercc.config import AuthConfig, ChannelsConfig, ClaudeConfig, Config
            config = Config(
                channels=ChannelsConfig(),
                auth=AuthConfig(),
                claude=ClaudeConfig(approved_directory=approved_directory),
            )
        self.feishu = feishu_client
        self.auth = authenticator
        self.validator = validator
        # Global Claude instance for command purposes only (e.g. /new resets session)
        # Actual queries are handled by per-chat-id SessionWorker instances
        self.claude = claude
        # Dedicated Claude instance for memory self-optimization — does not block main conversation
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
        # Group config: group_id -> GroupConfigEntry (for per-group access control)
        self._feishu_groups = feishu_groups or {}
        # Config path for auto-registering new groups
        self._config_path = config_path
        self.memory_manager = get_memory_manager()
        self.memory_manager.set_system_prompt_stale_callback(self._noop_mark_stale)
        self._skill_nudge = skill_nudge
        # Group chat history: chat_id -> list of recent message contents (max 20)
        self._group_history: dict[str, list[str]] = {}
        # Track which group chats we've already fetched history for (from Feishu API)
        self._fetched_group_chats: set[str] = set()
        # Per-chat-id worker pool
        self._session_workers: dict[str, SessionWorker] = {}
        self._workers_lock = asyncio.Lock()
        self._max_concurrent_workers = 10  # 最大并发 Worker 数
        self._current_message_id: str = ""

    def _noop_mark_stale(self) -> None:
        """No-op placeholder — SessionWorker.mark_system_prompt_stale is called instead"""
        pass

    def _trigger_memory_review(self, message: IncomingMessage, response_text: str) -> None:
        """Ask Claude to review conversation and update memory via MCP tools.

        Claude's tool calls (memory operations) are streamed directly to the user.
        """
        logger.info("[_trigger_memory_review] starting background review")

        prompt = (
            "根据之前的对话，判断是否有值得记住的信息。需要时直接调用 MCP 工具（新增/更新/删除）来管理记忆，不需要问我任何问题。\n"
        )

        async def do_review():
            # Dedicated instance — does not block main conversation
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
                        card = self._render_memory_card(result)
                        try:
                            await self.feishu.send_card(message.chat_id, card)
                        except Exception:
                            await self._safe_send(message.chat_id, message.message_id, str(card))
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
        """Get GroupConfigEntry for a chat_id, auto-registering if first seen."""
        if chat_id in self._feishu_groups:
            return self._feishu_groups[chat_id]

        # First time seeing this group — auto-register with defaults
        from supercc.config import GroupConfigEntry, register_group_config
        entry = GroupConfigEntry()
        self._feishu_groups[chat_id] = entry
        if self._config_path:
            try:
                await asyncio.to_thread(register_group_config, self._config_path, chat_id, entry)
                logger.info(f"Auto-registered new group {chat_id} in config")
            except Exception as ex:
                logger.warning(f"Failed to auto-register group {chat_id} in config: {ex}")
        return entry

    async def _check_group_access(self, message: IncomingMessage) -> bool:
        """Check if a group chat message should be processed.

        Returns True if allowed, False if should be skipped.
        Auto-registers new groups on first valid message.
        """
        if not message.is_group_chat:
            return True

        # Reject malformed messages with empty chat_id — not a valid group
        if not message.chat_id:
            logger.warning(f"Group chat message has empty chat_id, skipping")
            return False

        group_cfg = await self._get_group_config(message.chat_id)

        # If group is explicitly disabled, skip
        if group_cfg and not group_cfg.enabled:
            logger.info(f"Group {message.chat_id} is disabled in config, skipping")
            return False

        # If group has allow_from list, check sender
        if group_cfg and group_cfg.allow_from:
            if message.user_open_id not in group_cfg.allow_from:
                logger.info(f"User {message.user_open_id} not in group allow_from for {message.chat_id}, skipping")
                return False

        # If group has require_mention=False, bypass mention check (respond to all group messages)
        if group_cfg and not group_cfg.require_mention:
            return True

        # Default: require @CC mention for all group messages
        if not message.mention_bot:
            logger.info(f"Group chat message in {message.chat_id} without @CC mention, skipping")
            return False

        return True

    async def handle(self, message: IncomingMessage) -> HandlerResult:
        """Route message to command handler or per-chat-id worker pool."""

        # Group chat: record ALL messages to history FIRST, before any branching.
        # This ensures commands (/stop, /new, etc.) also get stored so that
        # when someone finally @mentions the bot, the full context is available.
        # On first seeing a chat, proactively fetch recent history from Feishu API
        # since WebSocket only delivers @mention messages.
        if message.is_group_chat and message.content:
            if message.chat_id not in self._fetched_group_chats:
                self._fetched_group_chats.add(message.chat_id)
                # Fetch last 20 messages from Feishu (ascending = chronological)
                raw_messages = await self.feishu.get_chat_history(
                    message.chat_id, limit=20, sort_type="ByCreateTimeAsc"
                )
                hist = self._group_history.setdefault(message.chat_id, [])
                for msg in raw_messages:
                    sender = msg.sender
                    if sender is None:
                        user_id = ""
                    elif isinstance(sender, dict):
                        sender_id = sender.get("sender_id", {}) or {}
                        user_id = sender_id.get("open_id", "") if isinstance(sender_id, dict) else ""
                    else:
                        # lark-oapi Sender object — has sender_id (UserID object) and sender_type
                        sid = getattr(sender, "sender_id", None)
                        user_id = getattr(sid, "open_id", "") if sid is not None else ""
                    msg_content = self.feishu._extract_content(msg)
                    if msg_content:
                        hist.append(f"{user_id}: {msg_content}")
                if len(hist) > 20:
                    hist[:] = hist[-20:]
                logger.debug(f"[GROUP_HISTORY][FETCH] chat_id={message.chat_id} fetched {len(raw_messages)} messages, hist_len={len(hist)}")

            hist = self._group_history.setdefault(message.chat_id, [])
            hist.append(f"{message.user_open_id}: {message.content}")
            if len(hist) > 20:
                hist[:] = hist[-20:]
            logger.debug(f"[GROUP_HISTORY][STORE] chat_id={message.chat_id} user={message.user_open_id} content={message.content!r} history_len={len(hist)}")

        # Commands are handled immediately — do not queue
        # BUT in group chat, require @CC mention (skip if some other bot was mentioned)
        if message.is_group_chat and not message.mention_bot:
            logger.info(f"Group command without @CC mention in {message.chat_id}, skipping")
            return HandlerResult(success=True)
        # Strip @mention prefix so '@_user_1 /git' is recognized as /git command
        content = _strip_mention_prefix(message.content)
        if content.startswith("/") and _is_command(content):
            # Authenticate first
            auth_result = self.auth.authenticate(message.user_open_id)
            if not auth_result.authorized:
                logger.info(f"Ignoring command from unauthorized user: {message.user_open_id}")
                return HandlerResult(success=True)
            result = await self._handle_command(message)
            if result.response_text:
                await self._safe_send(message.chat_id, message.message_id, result.response_text)
            return HandlerResult(success=True)

        # Route to per-chat-id worker
        worker = await self._get_or_create_worker(message.chat_id)
        await worker.queue.put(message)
        return HandlerResult(success=True)

    async def _get_or_create_worker(self, chat_id: str) -> SessionWorker:
        """Get or create a SessionWorker for the given chat_id."""
        async with self._workers_lock:
            if chat_id not in self._session_workers:
                active = [w for w in self._session_workers.values() if w._running]
                if len(active) >= self._max_concurrent_workers:
                    oldest = min(active, key=lambda w: w._idle_since or 0)
                    logger.warning(
                        f"[WORKER_LIMIT] chat_id={chat_id} 复用 worker={oldest.chat_id} "
                        f"(active={len(active)}, max={self._max_concurrent_workers})"
                    )
                    # 复用最旧 worker，更新其 chat_id 和 approved_directory
                    worker = oldest
                    worker.update_chat_id(chat_id)
                    self._session_workers[chat_id] = worker
                else:
                    self._session_workers[chat_id] = SessionWorker(chat_id, self)

            worker = self._session_workers[chat_id]
            if worker.task is None or worker.task.done():
                worker.task = asyncio.create_task(worker._run_loop())
            return worker

    
    async def _handle_command(self, message: IncomingMessage) -> HandlerResult:
        """Handle slash commands like /new, /status."""
        # Strip @mention prefix so commands work in group chat with @mention
        content = _strip_mention_prefix(message.content)
        parts = content.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/new":
            # 重置 options，continue_conversation=False 启动全新 session
            # 群聊时传入 chat_id，确保群聊 session 与 p2p session 隔离
            session = self.sessions.create_session(
                message.user_open_id,
                self.approved_directory,
                chat_id=message.chat_id if message.is_group_chat else None,
                platform="feishu",
            )
            # /new 需要设置到对应 chat_id 的 Worker 的 ClaudeIntegration
            # 如果 Worker 不存在，先创建
            worker = await self._get_or_create_worker(message.chat_id)
            worker._new_session_requested = True
            return HandlerResult(
                success=True,
                response_text=f"✅ 新会话已创建\n会话ID: {session.session_id}\n工作目录: {session.project_path}",
            )

        elif cmd == "/status":
            import os
            from supercc import __version__

            def _ver_gt(current: str, latest: str) -> bool:
                """简单版本比较：只比较数字段。"""
                import re
                def nums(v):
                    return [int(x) for x in re.findall(r'\d+', v)]
                return nums(latest) > nums(current)

            session = self.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id, platform="feishu")
            if not session:
                await self._safe_send(message.chat_id, message.message_id, "暂无活跃会话")
                return HandlerResult(success=True)

            # 获取当前模型信息（使用 ModelEnv 单例）
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

            # 获取 Git 分支
            try:
                import subprocess
                branch = subprocess.check_output(
                    ["git", "branch", "--show-current"],
                    text=True,
                    cwd=session.project_path,
                ).strip()
                if not branch:
                    branch = "(无分支)"
            except Exception:
                branch = "无分支"

            # 检查是否有新版本
            title = f"🐲龙王 **SuperCC v{__version__}**"
            try:
                from supercc.restarter import check_version
                current_ver, latest_ver = await asyncio.to_thread(check_version)
                if _ver_gt(current_ver, latest_ver):
                    title = f"🐲龙王 **SuperCC v{__version__} — 🌟可更新 v{latest_ver}🌟**"
            except Exception:
                pass  # 版本检查失败不影响主流程

            sdk_sid = session.sdk_session_id or "(未建立)"

            # 统计技能数量（每个技能是一个含 SKILL.md 的目录）
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

            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
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
                            ),
                        },
                    ]
                },
            }
            await self.feishu.send_card(message.chat_id, card)
            return HandlerResult(success=True)

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
                    "• /skill [all] — 查看技能列表（/skill all 查看全局）"
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
            return HandlerResult(
                success=True,
                response_text=f"未知命令: {cmd}",
            )


    async def _handle_restart(self, message: IncomingMessage) -> HandlerResult:
        from supercc.restarter import run_restart
        from supercc.main import _active_lock

        await self.feishu.add_typing_reaction(message.message_id)
        try:
            async for step in run_restart(_active_lock, self.feishu, message.chat_id, message.message_id):
                if step.status == "final":
                    # 成功完成，重启新进程已在运行
                    os._exit(0)
        except Exception as e:
            await self._safe_send(
                message.chat_id, message.message_id,
                f"❌ 重启失败: {e}"
            )

    async def _handle_update(self, message: IncomingMessage) -> HandlerResult:
        from supercc.restarter import run_update
        from supercc.main import _active_lock

        await self.feishu.add_typing_reaction(message.message_id)
        did_update = False
        try:
            did_update = await run_update(_active_lock, self.feishu, message.chat_id, message.message_id)
        except Exception as e:
            await self._safe_send(
                message.chat_id, message.message_id,
                f"❌ 更新失败: {e}"
            )
        # Only exit if an actual update (pip install) was performed
        if did_update:
            os._exit(0)
        return HandlerResult(success=True)

    async def _handle_memory(self, message: IncomingMessage) -> HandlerResult:
        """
        Handle /memory command.

        /memory                     — 列出所有指令说明
        /memory user add <title>|<content>|<keywords>
        /memory user del <id>
        /memory user update <id> <title>|<content>|<keywords>
        /memory user list
        /memory user search <query>
        /memory proj add <title>|<content>|<keywords>
        /memory proj del <id>
        /memory proj update <id> <title>|<content>|<keywords>
        /memory proj list
        /memory proj search <query>
        """
        parts = message.content.split(maxsplit=3)
        scope = parts[1].lower() if len(parts) > 1 else ""
        action = parts[2].lower() if len(parts) > 2 else ""
        raw_args = parts[3].strip() if len(parts) > 3 else ""

        # 无参数 → 显示指令说明
        if not scope:
            return HandlerResult(success=True, response_text=self._memory_help())

        # /memory user ...
        if scope == "user":
            return await self._handle_memory_user(message.user_open_id, action, raw_args)

        # /memory proj ...
        if scope == "proj":
            return await self._handle_memory_proj(message, action, raw_args)

        return HandlerResult(success=True,
                             response_text=f"未知 scope: {scope}\n"
                                           "用法: /memory [user|proj] <action> [参数]")

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
            "",
            "关键词用逗号分隔（若有多个）",
        ])

    # ── 记忆工具 MD 表格分页常量 ──────────────────────────────────────────────
    _MEM_PAGE_SIZE = 5

    def _render_memory_card(self, marker: _MemoryCardMarker) -> dict:
        """将 _MemoryCardMarker 渲染为 CardKit 原生格式（绕过 markdown 渲染）。"""
        try:
            args = json.loads(marker.tool_input) if marker.tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = marker.tool_name.replace("mcp__SuperCC__", "")
        scope = "proj" if "Proj" in short else "user"
        card_type = marker.card_type or ""

        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

        # ── 顶部文案 ─────────────────────────────────────────────────────
        header = f"🧠 **{short}**"
        if card_type == "search":
            q = args.get("query", "")
            header += f"  查询: 「{q}」"
        if scope == "proj":
            pp = args.get("project_path", "") or getattr(self, "_current_project_path", "")
            if pp:
                header += f"  项目: {pp.split('/')[-1] or pp}"
        elif args.get("user_open_id"):
            header += f"  用户: {args['user_open_id']}"

        elements = []

        # ── 内容体 ───────────────────────────────────────────────────────
        if card_type in ("add", "update"):
            # add/update → 条目表格（3列：标题、内容摘要、关键词）
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
            # list/search → 5列表格（#、标题、内容摘要、关键词、ID）
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
            # delete — 只展示被删记忆 ID
            deleted_id = marker.entries[0].get("id", "") if marker.entries else ""
            elements.append({"tag": "markdown", "content": f"{header}\n\n| ID |\n|------|\n| `{deleted_id}` |\n"})

        else:
            # fallback: 兜底参数表
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

    def _fmt_pref_table(self, prefs: list, total: int) -> str:
        """将用户偏好列表渲染为 MD 表格（一次性输出）。"""
        header = f"👤 **用户偏好**（共 {total} 条）\n\n"
        header += "| # | 标题 | 内容摘要 | 关键词 | ID |"
        header += "\n|---|------|----------|--------|---|"
        for i, p in enumerate(prefs, start=1):
            content_short = p.content[:60] + ("…" if len(p.content) > 60 else "")
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
            title_esc = esc(p.title)
            content_esc = esc(content_short)
            header += f"\n| {i} | {title_esc} | {content_esc} | {esc(p.keywords)} | `{p.id}` |"
        return header

    def _fmt_proj_table(self, mems: list, total: int) -> str:
        """将项目记忆列表渲染为 MD 表格（一次性输出）。"""
        header = f"📁 **项目记忆**（共 {total} 条）\n\n"
        header += "| # | 标题 | 内容摘要 | 关键词 | ID |"
        header += "\n|---|------|----------|--------|---|"
        for i, m in enumerate(mems, start=1):
            content_short = m.content[:60] + ("…" if len(m.content) > 60 else "")
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
            title_esc = esc(m.title)
            content_esc = esc(content_short)
            header += f"\n| {i} | {title_esc} | {content_esc} | {esc(m.keywords)} | `{m.id}` |"
        return header

    async def _handle_memory_user(self, user_open_id: str, action: str, raw_args: str) -> HandlerResult:
        """Handle /memory user <action>."""
        if action == "add":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory user add <title>|<content>|<keywords>")
            title = parts[0].strip()
            content = parts[1].strip()
            keywords = parts[2].strip()
            if not title or not content or not keywords:
                return HandlerResult(success=True, response_text="title、content、keywords 三样必填")
            platform = get_current_platform()
            p = self.memory_manager.add_preference(user_open_id, title, content, keywords, platform=platform)
            return HandlerResult(success=True,
                                 response_text=f"✅ 用户偏好已保存（ID: {p.id}）")

        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user del <id>")
            platform = get_current_platform()
            ok = self.memory_manager.delete_preference(raw_args, user_open_id=user_open_id, platform=platform)
            if ok:
                return HandlerResult(success=True, response_text=f"🗑️ 用户偏好 {raw_args} 已删除")
            return HandlerResult(success=True, response_text=f"未找到 id={raw_args} 的用户偏好")

        elif action == "update":
            parts = raw_args.split("|", 2)
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory user update <id> <title>|<content>|<keywords>")
            pref_id = parts[0].strip()
            title = parts[1].strip()
            content = parts[2].strip()
            keywords = ""
            if len(parts) > 3:
                keywords = parts[3].strip()
            if not pref_id or not title or not content:
                return HandlerResult(success=True, response_text="id、title、content 三样必填")
            platform = get_current_platform()
            ok = self.memory_manager.update_preference(pref_id, title, content, keywords, user_open_id=user_open_id, platform=platform)
            if ok:
                return HandlerResult(success=True, response_text=f"✅ 用户偏好 {pref_id} 已更新")
            return HandlerResult(success=True, response_text=f"未找到 id={pref_id} 的用户偏好")

        elif action == "list":
            platform = get_current_platform()
            prefs = self.memory_manager.get_preferences_by_user(user_open_id, platform=platform)
            if not prefs:
                return HandlerResult(success=True, response_text="📭 暂无用户偏好记录")
            return HandlerResult(success=True,
                                 response_text=self._fmt_pref_table(prefs, len(prefs)))

        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user search <关键词>")
            platform = get_current_platform()
            results = self.memory_manager.search_preferences(raw_args, user_open_id=user_open_id, platform=platform)
            if not results:
                return HandlerResult(success=True,
                                     response_text=f"未找到与「{raw_args}」相关的用户偏好")
            return HandlerResult(success=True,
                                 response_text=self._fmt_pref_table(results, len(results)))

        else:
            return HandlerResult(success=True,
                                 response_text=f"未知 user action: {action}\n"
                                               "用法: /memory user [add|del|update|list|search]")

    async def _handle_memory_proj(self, message: IncomingMessage, action: str, raw_args: str) -> HandlerResult:
        """Handle /memory proj <action>."""
        platform = get_current_platform()
        chat_id = message.chat_id or ""
        if action == "add":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory proj add <title>|<content>|<keywords>")
            title = parts[0].strip()
            content = parts[1].strip()
            keywords = parts[2].strip()
            if not title or not content or not keywords:
                return HandlerResult(success=True, response_text="title、content、keywords 三样必填")
            m = self.memory_manager.add_project_memory(
                self.approved_directory, title, content, keywords, platform=platform, chat_id=chat_id
            )
            return HandlerResult(success=True,
                                 response_text=f"✅ 项目记忆已保存（ID: {m.id}）")

        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj del <id>")
            ok = self.memory_manager.delete_project_memory(raw_args, self.approved_directory, platform=platform, chat_id=chat_id)
            if ok:
                return HandlerResult(success=True, response_text=f"🗑️ 项目记忆 {raw_args} 已删除")
            return HandlerResult(success=True, response_text=f"未找到 id={raw_args} 的项目记忆")

        elif action == "update":
            parts = raw_args.split("|", 2)
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory proj update <id> <title>|<content>|<keywords>")
            mem_id = parts[0].strip()
            title = parts[1].strip()
            content = parts[2].strip()
            keywords = ""
            if len(parts) > 3:
                keywords = parts[3].strip()
            if not mem_id or not title or not content:
                return HandlerResult(success=True, response_text="id、title、content 三样必填")
            ok = self.memory_manager.update_project_memory(mem_id, title, content, keywords, self.approved_directory, platform=platform, chat_id=chat_id)
            if ok:
                return HandlerResult(success=True, response_text=f"✅ 项目记忆 {mem_id} 已更新")
            return HandlerResult(success=True, response_text=f"未找到 id={mem_id} 的项目记忆")

        elif action == "list":
            mems = self.memory_manager.get_project_memories(self.approved_directory, platform=platform, chat_id=chat_id)
            if not mems:
                return HandlerResult(success=True, response_text="📭 暂无项目记忆记录")
            return HandlerResult(success=True,
                                 response_text=self._fmt_proj_table(mems, len(mems)))

        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj search <关键词>")
            results = self.memory_manager.search_project_memories(
                raw_args, self.approved_directory, platform=platform, chat_id=chat_id
            )
            if not results:
                return HandlerResult(success=True,
                                     response_text=f"未找到与「{raw_args}」相关的项目记忆")
            mems = [r.memory for r in results]
            return HandlerResult(success=True,
                                 response_text=self._fmt_proj_table(mems, len(mems)))

        else:
            return HandlerResult(success=True,
                                 response_text=f"未知 proj action: {action}\n"
                                               "用法: /memory proj [add|del|update|list|search]")

    async def _handle_skill(self, message: IncomingMessage) -> HandlerResult:
        """Handle /skill [all] command — list skills in project or globally."""
        parts = message.content.split(maxsplit=1)
        scope_all = len(parts) > 1 and parts[1].strip().lower() == "all"

        if scope_all:
            # 全局 skills: ~/.claude/skills/
            skills_dir = Path.home() / ".claude" / "skills"
            title = "全局 Skills"
        else:
            # 项目 skills: <project_path>/.supercc/skills/
            project_path = getattr(self, "_current_project_path", "") or self.approved_directory
            skills_dir = Path(project_path) / ".supercc" / "skills"
            title = f"项目 Skills（{Path(project_path).name}）"

        if not skills_dir.exists():
            return HandlerResult(
                success=True,
                response_text=f"📭 暂无 {'全局' if scope_all else '项目'} Skills\n"
                               f"目录不存在：{skills_dir}"
            )

        skill_entries = []
        for item in skills_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                # 读取 SKILL.md frontmatter 获取 name/description
                name = item.name
                description = ""
                try:
                    content = (item / "SKILL.md").read_text(encoding="utf-8")
                    # 解析 YAML frontmatter
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
            return HandlerResult(
                success=True,
                response_text=f"📭 暂无 {'全局' if scope_all else '项目'} Skills"
            )

        # 渲染 Markdown 表格
        lines = [
            f"## 🛠 {title}（共 {len(skill_entries)} 个）\n",
            "| 目录名 | Skill 名称 | 描述 |",
            "|--------|-----------|------|",
        ]
        for dirname, name, description in skill_entries:
            desc_short = description[:40] + "…" if len(description) > 40 else description
            lines.append(f"| `{dirname}` | {name} | {desc_short} |")

        return HandlerResult(success=True, response_text="\n".join(lines))

    async def _handle_switch(self, message: IncomingMessage) -> HandlerResult:
        """Handle /switch <target-path> command."""
        from supercc.switcher import run_switch, switch_to, SwitchError as SwitchErr

        parts = message.content.split(maxsplit=1)
        if len(parts) < 2:
            return HandlerResult(
                success=True,
                response_text="用法: /switch <目标项目路径>\n例: /switch /Users/x/my-project",
            )

        raw_path = parts[1].strip()
        if raw_path.startswith("/") or raw_path.startswith("~"):
            target = os.path.expanduser(raw_path)
        else:
            target = os.path.abspath(raw_path)

        await self.feishu.add_typing_reaction(message.message_id)

        try:
            await run_switch(target, self.feishu, message.chat_id, message.message_id)
        except SwitchErr as e:
            await self._safe_send(
                message.chat_id, message.message_id,
                f"❌ 切换失败\n\n**原因**: {e}"
            )

        return HandlerResult(success=True, response_text="")





    async def _handle_stop(self, message: IncomingMessage) -> HandlerResult:
        """Handle /stop — cancel the worker for this chat_id and interrupt Claude."""
        async with self._workers_lock:
            worker = self._session_workers.get(message.chat_id)
        if worker is None or not worker._running:
            await self._safe_send(message.chat_id, message.message_id, "当前没有正在运行的查询。")
            return HandlerResult(success=True)
        # Interrupt query FIRST, then cancel the worker task
        worker.claude.stop_event.set()
        if worker.task is not None and not worker.task.done():
            worker.task.cancel()
            worker.task = None
        await self._safe_send(message.chat_id, message.message_id, "🛑 已打断 Claude，当前任务已停止。")
        return HandlerResult(success=True)

    async def _handle_codex(self, message: IncomingMessage, arg: str = "") -> HandlerResult:
        """Handle /codex — Codex MCP status and setup. Direct execution removed in favor of MCP tool call."""
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

        # Default: show help
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

    async def _handle_model(self, message: IncomingMessage, subcmd: str = "") -> HandlerResult:
        """处理 /model 命令：显示所有供应商的模型配置（飞书卡片表格）。

        子命令：
        - /model switch <provider_id> <model_id> — 切换到指定供应商的模型
        """
        from supercc.claude.model_config import (
            get_all_providers,
            get_model_env,
            set_project_model,
        )
        from supercc.claude.model_providers import PROVIDERS

        env = get_model_env()  # 直接从单例拿，不用查 model.json

        # 处理子命令
        if subcmd:
            parts = subcmd.strip().split(maxsplit=2)
            action = parts[0].lower()
            target_pid = parts[1] if len(parts) > 1 else ""
            target_model = parts[2] if len(parts) > 2 else ""

            if action == "switch":
                if not target_pid:
                    available = " / ".join(f"`{p.id}`" for p in PROVIDERS.values() if p.id != "custom")
                    await self._safe_send(
                        message.chat_id, message.message_id,
                        f"❌ 请指定要切换的 provider ID。\n可用：\n{available}",
                    )
                    return HandlerResult(success=True)

                provider = PROVIDERS.get(target_pid)
                if not provider:
                    available = " / ".join(f"`{p.id}`" for p in PROVIDERS.values() if p.id != "custom")
                    await self._safe_send(
                        message.chat_id, message.message_id,
                        f"❌ 未知 provider `{target_pid}`。\n可用：\n{available}",
                    )
                    return HandlerResult(success=True)

                if not target_model:
                    await self._safe_send(
                        message.chat_id, message.message_id,
                        f"❌ 请指定模型 ID。\n可用模型：\n{' / '.join(f'`{m}`' for m in provider.models)}",
                    )
                    return HandlerResult(success=True)

                if target_model not in provider.models:
                    await self._safe_send(
                        message.chat_id, message.message_id,
                        f"❌ 模型 ID `{target_model}` 不在供应商 `{provider.id}` 的可用模型列表中。\n可用模型：\n{' / '.join(f'`{m}`' for m in provider.models)}",
                    )
                    return HandlerResult(success=True)

                ok, err = set_project_model(self.data_dir, target_pid, target_model)
                if not ok:
                    await self._safe_send(
                        message.chat_id, message.message_id,
                        f"❌ 切换失败：{err}",
                    )
                    return HandlerResult(success=True)

                await self._safe_send(
                    message.chat_id, message.message_id,
                    f"✅ 已切换为 `{provider.id}`（模型：`{target_model}`）",
                )
                return HandlerResult(success=True)

        # 默认：显示卡片表格
        # 直接从单例获取当前激活的模型，不用查 model.json
        current_mid = env.ANTHROPIC_MODEL
        current_pid = env.provider_id  # ModelEnv 直接包含 provider_id
        providers_cfg = get_all_providers()  # 从 model.json 拿供应商 API Key 配置

        configured = []
        unconfigured = []

        for pid, provider in PROVIDERS.items():
            if pid == "custom":
                continue
            pcfg = providers_cfg.get(pid)
            api_key = pcfg.api_key if pcfg else ""
            if api_key:
                configured.append((pid, provider.id, api_key, current_mid or "—", provider.models, pid == current_pid))
            else:
                unconfigured.append((pid, provider.id, provider.models))

        def mask_api_key(key: str) -> str:
            if not key:
                return "—"
            if len(key) <= 10:
                return "****"
            return key[:6] + "***" + key[-4:]

        def fmt_models(models: list[str], current: str) -> str:
            parts = []
            for m in models:
                if m == current:
                    parts.append(f"**`{m}`**")
                else:
                    parts.append(f"`{m}`")
            return " / ".join(parts)

        # 当前激活的条目放最前面
        configured.sort(key=lambda x: 0 if x[5] else 1)

        table_header = "| 状态 | Provider | API Key | 所有可用模型 |"
        table_sep = "|------|----------|---------|------------|"

        active_name = "未设置"
        if current_pid:
            p = PROVIDERS.get(current_pid)
            active_name = p.id if p else current_pid

        table_lines = [table_header, table_sep]
        for pid, pname, api_key, model, all_models, is_active in configured:
            mark = "✅" if is_active else "✴️"
            avail = fmt_models(all_models, model)
            table_lines.append(f"| {mark} | `{pid}` | `{mask_api_key(api_key)}` | {avail} |")
        for pid, pname, all_models in unconfigured:
            avail = " / ".join(f"`{m}`" for m in all_models)
            table_lines.append(f"| 📛 | `{pid}` | — | {avail} |")
        table_content = "\n".join(table_lines)

        elements = [
            {
                "tag": "markdown",
                "content": (
                    "## 🤖 模型配置\n"
                    f"当前使用：**{active_name}**（`{current_mid or '未设置'}`）\n\n"
                    f"共 **{len(configured)}** 个已配置，**{len(unconfigured)}** 个未配置。\n\n"
                    + table_content
                    + "\n\n---\n💡 切换模型：`/model switch <provider_id> <model_id>`\n或直接对我说：帮我切换到&lt;供应商&gt;的&lt;模型id&gt;模型"
                ),
            },
        ]

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": elements},
        }

        try:
            await self.feishu.send_card(message.chat_id, card)
        except Exception:
            text = [f"🤖 **模型配置**\n"]
            for pid, pname, api_key, model, all_models, is_active in configured:
                m = "✅" if is_active else "✴️"
                text.append(f"{m} {pname}: {model} | {mask_api_key(api_key)}")
            for pid, pname, all_models in unconfigured:
                avail = ", ".join(all_models[:4])
                text.append(f"📛 {pname}: {avail}...")
            text.append(f"\n共{len(configured)}个已配置，{len(unconfigured)}个未配置。\n💡 切换：`/model switch <provider_id> <model_id>`\n或直接对我说：帮我切换到<供应商>的<模型id>模型")
            await self._safe_send(message.chat_id, message.message_id, "\n".join(text))

        return HandlerResult(success=True)

    async def _handle_git(self, message: IncomingMessage) -> HandlerResult:
        """执行 git status 和 log，返回精美卡片。"""
        import subprocess

        def run_git(args: list[str]) -> str:
            try:
                result = subprocess.run(
                    ["git"] + args,
                    capture_output=True, text=True, timeout=10,
                    cwd=self.approved_directory
                )
                return result.stdout.strip()
            except Exception:
                return ""

        # 当前分支
        branch = run_git(["branch", "--show-current"])
        if not branch:
            branch = "(无分支)"

        # 变更文件
        status_output = run_git(["status", "--porcelain"])

        # 最近 5 次提交: ISO时间 + hash(7位) + 描述
        # %cI = ISO 8601，无空格，split 不易错位
        log_lines = run_git(["log", "--format=%cI %h %s", "-5"]).splitlines()

        # 构建单条 markdown 内容
        card_lines = [
            f"🌟 **Git Status - {branch}**",
            "",
            "📝 **变更文件**",
        ]

        # git 状态字母到颜色的映射
        status_color = {
            "A": "green",  # Added
            "M": "orange", # Modified
            "D": "red",    # Deleted
            "R": "purple", # Renamed
            "U": "red",    # Unmerged
            "C": "gray",   # Copied
            "?": "gray",   # Untracked
        }

        has_changes = bool(status_output)
        if has_changes:
            for line in status_output.splitlines():
                idx_char = line[0]
                wt_char = line[1]
                if idx_char == "?":
                    char = "?"
                elif idx_char == " ":
                    char = wt_char if wt_char != " " else "?"
                else:
                    char = idx_char
                color = status_color.get(char, "gray")
                filename = line[3:]
                card_lines.append(f"<font color='{color}'>{char}</font> {filename}")
        else:
            card_lines.append("✅ 工作区干净，无待提交变更")

        # 最近提交始终显示
        card_lines.extend([
            "",
            "📋 **最近提交**",
            "",
            "| 时间 | Hash | 描述 |",
            "|------|------|------|",
        ])
        for log_line in log_lines:
            parts = log_line.split(" ", 2)
            if len(parts) >= 3:
                dt_clean = parts[0].replace("T", " ")[:16]
                h = parts[1]
                msg = parts[2]
                card_lines.append(f"| {dt_clean} | `{h}` | {msg} |")

        card_body = "\n".join(card_lines)
        try:
            await self.feishu.send_interactive_reply(
                message.chat_id, card_body, message.message_id, log_reply=True
            )
        except Exception:
            await self._safe_send(message.chat_id, message.message_id, card_body)

        return HandlerResult(success=True)

    async def _safe_send(self, chat_id: str, reply_to_message_id: str, text: str, log_reply: bool = True, preformatted: bool = False):
        """Send a markdown message as a threaded Feishu post/card, ignoring errors.

        Uses Interactive Card for content with fenced code blocks or tables,
        falls back to rich text post for plain markdown.
        """
        try:
            # Optimize and decide format — skip if already formatted to avoid double-processing
            formatted = text if preformatted else self.formatter.format_text(text)
            if not formatted.strip():
                return
            if self.formatter.should_use_card(formatted):
                await self.feishu.send_interactive_reply(chat_id, formatted, reply_to_message_id, log_reply=log_reply)
            else:
                await self.feishu.send_post_reply(chat_id, formatted, reply_to_message_id, log_reply=log_reply)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")

    def _extract_quoted_content(self, message: dict) -> str:
        """Extract text content from a fetched message dict."""
        msg_type = message.get("msg_type", "")
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
            if msg_type == "text":
                return content.get("text", "")
            elif msg_type == "post":
                return content.get("text", "")
        except Exception:
            pass
        return str(content_str)

    async def _preprocess_media(self, message: IncomingMessage) -> str:
        """Download and save inbound media, return the text to prepend to prompt.

        Returns:
            空字符串（无媒体），或形如 "![image](path)" / "[File: path]" 等格式的文本片段。
            图片用 markdown image 语法以便 SDK 的 detectAndLoadPromptImages 识别；
            文件/音频用 [File: /path] / [Audio: /path] 格式告知 AI 附件内容，
            AI 会通过 Read 工具读取本地文件。
        """
        from supercc.adapter.feishu.media import (
            make_image_path,
            make_file_path,
            save_bytes,
        )

        if message.message_type not in ("image", "file"):
            return ""

        msg_id = message.message_id
        logger.info(f"[media] preprocessing {message.message_type} message {msg_id}")

        # Use get_message API to get reliable content (WS event content may be
        # missing image_key for image messages — API always returns it correctly).
        msg_data = await self.feishu.get_message(msg_id)
        if not msg_data:
            logger.warning(f"[media] failed to fetch message {msg_id}")
            return ""
        content_str = msg_data.get("content", "{}")
        logger.debug(f"[media] got content: {content_str[:200]!r}")

        try:
            content = json.loads(content_str)
        except Exception:
            logger.warning(f"[media] json.loads failed on {content_str!r}")
            return ""

        data_dir = self.data_dir or os.getcwd()

        def _find_first_image_key(parsed: dict) -> str | None:
            """Find first image_key in simple or rich post content format."""
            # Simple: {"image_key": "..."}
            if "image_key" in parsed:
                return parsed.get("image_key")
            # Rich post: {"content": [[{"tag": "img", "image_key": "..."}]]}
            for block in parsed.get("content", []):
                if not isinstance(block, list):
                    continue
                for item in block:
                    if isinstance(item, dict) and item.get("tag") == "img":
                        return item.get("image_key")
            return None

        if message.message_type == "image":
            file_key = _find_first_image_key(content)
            if not file_key:
                logger.warning(f"[media] no image_key in message {msg_id}")
                return ""
            logger.info(f"[media] downloading image, key={file_key}")
            base_path = make_image_path(data_dir, msg_id)
            data = await self.feishu.download_media(msg_id, file_key, msg_type="image")
            save_path = base_path + ".png"
            save_bytes(save_path, data)
            logger.info(f"[media] saved image to {save_path}")
            # Use standard markdown image syntax so Claude CLI's detectAndLoadPromptImages
            # recognizes the local path. The SDK scans for "![alt](path)" with an image extension.
            return f"![image]({save_path})"

        elif message.message_type == "file":
            file_key = content.get("file_key", "")
            orig_name = content.get("file_name", "file")
            file_type = content.get("file_type", "bin")
            if not file_key:
                logger.warning(f"[media] no file_key in message {msg_id}")
                return ""
            logger.info(f"[media] downloading file {orig_name}, key={file_key}")
            save_path = make_file_path(data_dir, msg_id, orig_name, file_type)
            data = await self.feishu.download_media(msg_id, file_key, msg_type="file")
            save_bytes(save_path, data)
            logger.info(f"[media] saved file to {save_path}")
            # [File: /path] 告知 AI 收到了文件，AI 会用 Read 工具读取。
            # 包含原始文件名方便 AI 判断文件类型和内容。
            return f"[File: {save_path}] ({orig_name})"

        return ""

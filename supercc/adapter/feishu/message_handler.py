"""Message handler orchestrator — routes messages to Claude and back."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from supercc.adapter.feishu.client import FeishuClient, IncomingMessage
from supercc.security.auth import Authenticator
from supercc.security.validator import SecurityValidator
from supercc.claude.integration import ClaudeIntegration
from supercc.claude.memory_manager import get_memory_manager, MEMORY_SYSTEM_GUIDE
from supercc.claude.feishu_file_tools import FEISHU_FILE_GUIDE
from supercc.claude.cron_tools import CRON_GUIDE
from supercc.claude.session_manager import SessionManager
from supercc.evolve.skill_nudge import SkillNudge, trigger_skill_review
from supercc.adapter.feishu.format.reply_formatter import ReplyFormatter
from supercc.adapter.feishu.format.edit_diff import _DiffMarker, _MemoryCardMarker
from supercc.adapter.feishu.format.questionnaire_card import _AskUserQuestionMarker, format_questionnaire_card

logger = logging.getLogger(__name__)

# Match a slash-command like "/stop", "/new", "/feishu auth", "/status foo"
# Commands: / + letter + word-chars, optionally followed by space + args
# NOT a path: paths contain slashes later (e.g. /Users/x/...)
_COMMAND_RE = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_-]*(?:\s.*)?$")


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
        config,
        data_dir: str = "",
        feishu_groups: dict | None = None,
        config_path: str | None = None,
        skill_nudge: SkillNudge | None = None,
    ):
        self.feishu = feishu_client
        self.auth = authenticator
        self.validator = validator
        self.claude = claude
        # Dedicated Claude instance for memory self-optimization — does not block main conversation
        self.claude_memory = ClaudeIntegration(
            cli_path=config.claude.cli_path,
            max_turns=5,
            approved_directory=approved_directory,
        )
        # Dedicated Claude instance for skill self-evolution — separate session, does not block
        self.claude_skill = ClaudeIntegration(
            cli_path=config.claude.cli_path,
            max_turns=5,
            approved_directory=approved_directory,
        )
        self.sessions = session_manager
        self.formatter = formatter
        self.approved_directory = approved_directory
        self.data_dir = data_dir
        # Group config: group_id -> GroupConfigEntry (for per-group access control)
        self._feishu_groups = feishu_groups or {}
        # Config path for auto-registering new groups
        self._config_path = config_path
        self.memory_manager = get_memory_manager()
        self.memory_manager.set_system_prompt_stale_callback(self.claude.mark_system_prompt_stale)
        self._skill_nudge = skill_nudge
        self._queue: asyncio.Queue[IncomingMessage] | None = None
        self._queue_loop_id: int | None = None
        # Group chat history: chat_id -> list of recent message contents (max 20)
        self._group_history: dict[str, list[str]] = {}
        # Track which group chats we've already fetched history for (from Feishu API)
        self._fetched_group_chats: set[str] = set()
        self._worker_task: asyncio.Task | None = None
        self._is_processing: bool = False  # True while worker is running or about to run
        self._current_message_id: str = ""

    def _get_queue(self) -> asyncio.Queue[IncomingMessage]:
        """Lazily create (or recreate) the queue in the current event loop.

        If the event loop has changed since the queue was created (e.g., after
        tests switch loops), discard the stale queue and create a fresh one.
        """
        try:
            current_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            current_loop_id = None
        if self._queue is None or self._queue_loop_id != current_loop_id:
            self._queue = asyncio.Queue()
            self._queue_loop_id = current_loop_id
        return self._queue

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
                    )
                    if isinstance(result, _MemoryCardMarker):
                        card_md = self._render_memory_card(result)
                        await self._safe_send(message.chat_id, message.message_id, self.formatter.format_text(card_md))
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

    def _get_group_config(self, chat_id: str):
        """Get GroupConfigEntry for a chat_id, auto-registering if first seen."""
        if chat_id in self._feishu_groups:
            return self._feishu_groups[chat_id]

        # First time seeing this group — auto-register with defaults
        from supercc.config import GroupConfigEntry, register_group_config
        entry = GroupConfigEntry()
        self._feishu_groups[chat_id] = entry
        if self._config_path:
            try:
                register_group_config(self._config_path, chat_id, entry)
                logger.info(f"Auto-registered new group {chat_id} in config")
            except Exception as ex:
                logger.warning(f"Failed to auto-register group {chat_id} in config: {ex}")
        return entry

    def _check_group_access(self, message: IncomingMessage) -> bool:
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

        group_cfg = self._get_group_config(message.chat_id)

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
        """将消息入队，立即返回。由 Worker 串行处理。

        注意：所有命令（/开头）都不入队，直接处理以确保立即响应。
        """
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

        queue = self._get_queue()
        await queue.put(message)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
            # Set _is_processing immediately (before the coroutine even runs) so that
            # a concurrent /stop command sees it as True and can interrupt correctly.
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon(lambda: setattr(self, "_is_processing", True))
            except RuntimeError:
                pass
        return HandlerResult(success=True)

    def _init_options(
        self,
        system_prompt_append: str | None = None,
        continue_conversation: bool = True,
    ) -> None:
        """
        初始化/更新持久化 options。
        system prompt 更新只需重新调用此方法。
        """
        self.claude._init_options(system_prompt_append, continue_conversation)

    async def _worker_loop(self) -> None:
        """串行出队并处理消息。"""
        try:
            while True:
                try:
                    queue = self._get_queue()
                    message = await queue.get()
                    try:
                        self._current_message_id = message.message_id
                        await self._process_message(message)
                    finally:
                        self._current_message_id = ""
                        queue.task_done()
                except asyncio.CancelledError:
                    break
                except RuntimeError as e:
                    # Queue bound to a different event loop (e.g., after test teardown) — exit silently.
                    # Only swallow the specific queue/loop errors; re-raise everything else.
                    err_msg = str(e)
                    if "different event loop" in err_msg or "Event loop is closed" in err_msg:
                        break
                    raise  # re-raise unknown RuntimeError
                except Exception:
                    logger.exception("Worker loop error")
        finally:
            self._is_processing = False

    async def _process_message(self, message: IncomingMessage) -> None:
        """处理单条消息：鉴权 → 媒体预处理 → 引用检测 → 查询。"""
        # P2P: allowed_users whitelist applies. Group @mention: controlled by GroupConfigEntry.
        if not message.is_group_chat:
            auth_result = self.auth.authenticate(message.user_open_id)
            if not auth_result.authorized:
                logger.info(f"Ignoring message from unauthorized user: {message.user_open_id}")
                return

        # Group chat: skip if bot was not @mentioned (no response to avoid spam)
        # Group access control check (per-group config: enabled, allow_from, require_mention)
        if not self._check_group_access(message):
            return

        if message.message_type not in ("text", "image", "file"):
            await self._safe_send(message.chat_id, message.message_id, "暂不支持该消息类型，请发送文字消息。")
            return

        # Only validate text content — media messages (image/file) have empty
        # content at this stage and will get their path-injected content in _run_query.
        # NOTE: SecurityValidator pattern checks are currently disabled.
        # To re-enable: uncomment the block below.
        # if message.message_type == "text":
        #     ok, err = self.validator.validate(message.content)
        #     if not ok:
        #         await self._safe_send(message.chat_id, message.message_id, f"⚠️ {err}")
        #         return

        # For group chat, use chat-specific session lookup to isolate group sessions
        # from p2p sessions. For p2p, use the standard user-level session.
        if message.is_group_chat:
            session = self.sessions.get_active_session_for_chat(message.user_open_id, message.chat_id)
            if session is None:
                # First message in this group chat — create a new session
                session = self.sessions.create_session(
                    message.user_open_id,
                    self.approved_directory,
                    chat_id=message.chat_id,
                )
            elif session.chat_id != message.chat_id:
                # Same user in a different group — update session to point to new chat
                self.sessions.update_chat_id(message.user_open_id, message.chat_id)
        else:
            session = self.sessions.get_active_session(message.user_open_id)
            if session and session.chat_id != message.chat_id:
                self.sessions.update_chat_id(message.user_open_id, message.chat_id)

        project_path = session.project_path if session else self.approved_directory
        self._current_project_path = project_path  # 供 stream_callback 使用

        system_prompt_append = (
            MEMORY_SYSTEM_GUIDE
            + FEISHU_FILE_GUIDE
            + CRON_GUIDE
            + self.memory_manager.inject_context(
                user_open_id=message.user_open_id,
                project_path=project_path,
            )
        )

        # 确保 options 已初始化
        self._init_options(system_prompt_append)

        await self._run_query(message, session)

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
            )
            self._init_options(continue_conversation=False)
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

            session = self.sessions.get_active_session(message.user_open_id)
            if not session:
                await self._safe_send(message.chat_id, message.message_id, "暂无活跃会话")
                return HandlerResult(success=True)

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
            return await self._handle_model(message)

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
            await run_restart(_active_lock, self.feishu, message.chat_id, message.message_id)
        except Exception as e:
            await self._safe_send(
                message.chat_id, message.message_id,
                f"❌ 重启失败: {e}"
            )
        os._exit(0)

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

    async def _handle_skill(self, message: IncomingMessage) -> HandlerResult:
        """Handle /skill [all] — list project or global skills."""
        parts = message.content.split(maxsplit=1)
        scope = parts[1].strip().lower() if len(parts) > 1 else ""

        if scope == "all":
            skills_dir = os.path.expanduser("~/.claude/skills")
            label = "全局"
        else:
            session = self.sessions.get_active_session(message.user_open_id)
            project_path = session.project_path if session else self.approved_directory
            skills_dir = os.path.join(project_path, ".supercc", "skills")
            label = "项目"

        skills = self._scan_skills_dir(skills_dir)

        if not skills:
            return HandlerResult(success=True, response_text=f"📭 暂无可用的{label} Skill")

        return HandlerResult(success=True, response_text=self._fmt_skills_table(skills, label))

    def _scan_skills_dir(self, skills_dir: str) -> list[dict]:
        """扫描 Skill 目录，返回 Skill 列表（含 name/description/author/version）。"""
        import re
        skills = []
        p = Path(skills_dir)
        if not p.exists():
            return skills

        for item in p.iterdir():
            if not item.is_dir():
                continue
            skill_md = item / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
                fm = {}
                if content.startswith("---"):
                    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                    if match:
                        for line in match.group(1).splitlines():
                            if ":" in line:
                                key, val = line.split(":", 1)
                                fm[key.strip()] = val.strip()
                skills.append({
                    "name": fm.get("name", item.name),
                    "description": fm.get("description", ""),
                    "author": fm.get("author", ""),
                    "version": fm.get("version", ""),
                })
            except Exception:
                continue
        return skills

    def _fmt_skills_table(self, skills: list[dict], label: str) -> str:
        """将 Skill 列表渲染为 Markdown 表格。"""
        def _esc_cell(s: str) -> str:
            return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")[:60]

        lines = [f"## 🛠️ {label} Skill（共 {len(skills)} 个）\n"]
        lines.append("\n| 名称 | 描述 | 作者 | 版本 |")
        lines.append("|------|------|------|------|")
        for s in skills:
            name = _esc_cell(s.get("name", ""))
            desc = _esc_cell(s.get("description") or "")
            author = _esc_cell(s.get("author", ""))
            version = _esc_cell(s.get("version", ""))
            lines.append(f"| {name} | {desc} | {author} | {version} |")
        return "\n".join(lines)

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
            return await self._handle_memory_proj(action, raw_args)

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

    def _render_memory_card(self, marker: _MemoryCardMarker) -> str:
        """将 _MemoryCardMarker 渲染为 MD 字符串。"""
        try:
            args = json.loads(marker.tool_input) if marker.tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = marker.tool_name.replace("mcp__SuperCC__", "")
        scope = "proj" if "Proj" in short else "user"
        card_type = marker.card_type or ""

        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

        def _entry_table(entries: list, show_num: bool = False) -> str:
            if not entries:
                return "  _无结果_"
            lines = "|"
            sep = "|"
            cols = ["标题", "内容摘要", "关键词", "ID"]
            if show_num:
                cols = ["#"] + cols
            for c in cols:
                lines += f" {c} |"
                sep += "------|"
            lines += "\n" + sep + "\n"
            for i, e in enumerate(entries):
                num = str(i + 1) if show_num else ""
                title = _esc(e.get("title", "")[:40])
                content = _esc(e.get("content", "")[:50])
                keywords = _esc(e.get("keywords", ""))
                mid = f"`{e.get('id', '')}`"
                if show_num:
                    lines += f"| {num} | {title} | {content} | {keywords} | {mid} |\n"
                else:
                    lines += f"| {title} | {content} | {keywords} | {mid} |\n"
            return lines

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
        # add/update 的 title 已在表格中展示，顶部文案不再重复

        # ── 内容体 ───────────────────────────────────────────────────────
        if card_type in ("add", "update"):
            # add/update → 条目表格，标题列置顶（顶部文案不含 title）
            lines = f"{header}\n\n| 标题 | 内容摘要 | 关键词 |\n|------|----------|--------|\n"
            for e in marker.entries:
                title = _esc(e.get("title", "")[:60])
                content = _esc(e.get("content", "")[:50])
                keywords = _esc(e.get("keywords", ""))
                mid = f"`{e.get('id', '')}`"
                lines += f"| {title} | {content} | {keywords} |\n"
            return lines

        elif card_type in ("list", "search"):
            label = "项目记忆" if scope == "proj" else "用户偏好"
            total = len(marker.entries)
            header += f"（共 {total} 条）"
            body = _entry_table(marker.entries, show_num=True)
            return f"{header}\n\n{body}"

        elif card_type == "delete":
            # delete — 只展示被删记忆 ID
            deleted_id = marker.entries[0].get("id", "") if marker.entries else ""
            return f"{header}\n\n| ID |\n|------|\n| `{deleted_id}` |\n"

        # fallback: 兜底参数表
        lines = f"{header}\n\n| 参数 | 值 |\n|------|----|\n"
        for k, v in args.items():
            v_str = _esc(str(v))
            if len(v_str) > 80:
                v_str = v_str[:80] + "…"
            lines += f"| `{k}` | {v_str} |\n"
        return lines

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
            p = self.memory_manager.add_preference(user_open_id, title, content, keywords)
            return HandlerResult(success=True,
                                 response_text=f"✅ 用户偏好已保存（ID: {p.id}）")

        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user del <id>")
            ok = self.memory_manager.delete_preference(raw_args)
            if ok:
                return HandlerResult(success=True, response_text=f"🗑️ 用户偏好 {raw_args} 已删除")
            return HandlerResult(success=True, response_text=f"未找到 id={raw_args} 的用户偏好")

        elif action == "update":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory user update <id> <title>|<content>|<keywords>")
            pref_id = parts[0].strip()
            title = parts[1].strip()
            content = parts[2].strip()
            keywords = parts[3].strip() if len(parts) > 3 else ""
            if not pref_id or not title or not content:
                return HandlerResult(success=True, response_text="id、title、content 三样必填")
            ok = self.memory_manager.update_preference(pref_id, title, content, keywords)
            if ok:
                return HandlerResult(success=True, response_text=f"✅ 用户偏好 {pref_id} 已更新")
            return HandlerResult(success=True, response_text=f"未找到 id={pref_id} 的用户偏好")

        elif action == "list":
            prefs = self.memory_manager.get_all_preferences()
            if not prefs:
                return HandlerResult(success=True, response_text="📭 暂无用户偏好记录")
            return HandlerResult(success=True,
                                 response_text=self._fmt_pref_table(prefs, len(prefs)))

        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory user search <关键词>")
            results = self.memory_manager.search_preferences(raw_args)
            if not results:
                return HandlerResult(success=True,
                                     response_text=f"未找到与「{raw_args}」相关的用户偏好")
            return HandlerResult(success=True,
                                 response_text=self._fmt_pref_table(results, len(results)))

        else:
            return HandlerResult(success=True,
                                 response_text=f"未知 user action: {action}\n"
                                               "用法: /memory user [add|del|update|list|search]")

    async def _handle_memory_proj(self, action: str, raw_args: str) -> HandlerResult:
        """Handle /memory proj <action>."""
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
                self.approved_directory, title, content, keywords
            )
            return HandlerResult(success=True,
                                 response_text=f"✅ 项目记忆已保存（ID: {m.id}）")

        elif action == "del":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj del <id>")
            ok = self.memory_manager.delete_project_memory(raw_args)
            if ok:
                return HandlerResult(success=True, response_text=f"🗑️ 项目记忆 {raw_args} 已删除")
            return HandlerResult(success=True, response_text=f"未找到 id={raw_args} 的项目记忆")

        elif action == "update":
            parts = raw_args.split("|")
            if len(parts) < 3:
                return HandlerResult(success=True,
                                     response_text="用法: /memory proj update <id> <title>|<content>|<keywords>")
            mem_id = parts[0].strip()
            title = parts[1].strip()
            content = parts[2].strip()
            keywords = parts[3].strip() if len(parts) > 3 else ""
            if not mem_id or not title or not content:
                return HandlerResult(success=True, response_text="id、title、content 三样必填")
            ok = self.memory_manager.update_project_memory(mem_id, title, content, keywords)
            if ok:
                return HandlerResult(success=True, response_text=f"✅ 项目记忆 {mem_id} 已更新")
            return HandlerResult(success=True, response_text=f"未找到 id={mem_id} 的项目记忆")

        elif action == "list":
            mems = self.memory_manager.get_project_memories(self.approved_directory)
            if not mems:
                return HandlerResult(success=True, response_text="📭 暂无项目记忆记录")
            return HandlerResult(success=True,
                                 response_text=self._fmt_proj_table(mems, len(mems)))

        elif action == "search":
            if not raw_args:
                return HandlerResult(success=True, response_text="用法: /memory proj search <关键词>")
            results = self.memory_manager.search_project_memories(
                raw_args, self.approved_directory
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
            project_path = self._current_project_path or self.approved_directory
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




    async def _run_query(
        self,
        message: IncomingMessage,
        session,
    ) -> None:
        """Run Claude query in background, send results to Feishu on completion."""
        reaction_id = None
        _last_response = ""

        async def _show_typing() -> None:
            """在 _query_lock 拿到后显示 typing（通过 on_start 回调传入 query）。"""
            nonlocal reaction_id
            reaction_id = await self.feishu.add_typing_reaction(message.message_id)
            logger.info(f"[typing] on — user={message.user_open_id}, reaction_id={reaction_id!r}")

        try:

            # Audio is not yet supported — tell the user and skip Claude
            if message.message_type == "audio":
                await self._safe_send(message.chat_id, message.message_id, "🎙️ 暂不支持语音消息，请发送文字消息。")
                return

            # Preprocess media (image/file) before querying Claude
            media_prompt_prefix = ""
            media_notify_text = ""
            logger.debug(f"[_run_query] message_type={message.message_type!r}")
            if message.message_type in ("image", "file"):
                logger.debug(f"[_run_query] entering media branch for {message.message_type}")
                try:
                    media_prompt_prefix = await self._preprocess_media(message)
                    if media_prompt_prefix:
                        logger.info(f"Inbound media saved: {media_prompt_prefix}")
                        # Notify user in Feishu that media was received
                        icon = {"image": "🖼️", "file": "🗃"}.get(message.message_type, "🗃")
                        media_notify_text = f"{icon} 收到 {message.message_type}，正在分析..."
                        await self._safe_send(message.chat_id, message.message_id, media_notify_text)
                except Exception as e:
                    logger.warning(f"Failed to process inbound media: {e}")
                    media_prompt_prefix = ""

            # Resolve quoted message content
            quoted_content = ""
            if message.parent_id:
                try:
                    quoted_msg = await self.feishu.get_message(message.parent_id)
                    if quoted_msg:
                        sender_id = quoted_msg.get("sender_id", "")
                        quoted_text = self._extract_quoted_content(quoted_msg)
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
                hist = self._group_history.get(message.chat_id, [])
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
            for retry_round in range(3):
                accumulator = StreamAccumulator(message.chat_id, message.message_id, self._safe_send)

                async def stream_callback(claude_msg):
                    if claude_msg.tool_name:
                        await accumulator.flush()
                        # 记忆工具传入 memory_manager 和默认 project_path
                        kwargs = {}
                        if claude_msg.tool_name.startswith("mcp__SuperCC__Memory"):
                            kwargs["memory_manager"] = self.memory_manager
                            kwargs["default_project_path"] = getattr(self, "_current_project_path", "")
                        result = self.formatter.format_tool_call(
                            claude_msg.tool_name,
                            claude_msg.tool_input,
                            **kwargs,
                        )
                        logger.info(f"[stream] tool: {claude_msg.tool_name} | input: {claude_msg.tool_input}")

                        # Hermes-style skill nudge: count tool calls (trigger after query completes)
                        nudge = self._skill_nudge
                        if nudge:
                            nudge.config.current_user = message.user_open_id
                            nudge.increment()

                        # _DiffMarker / list[_DiffMarker] → 彩色卡片；其他 → backtick 格式
                        if isinstance(result, _DiffMarker):
                            for card in result.card if isinstance(result.card, list) else [result.card]:
                                try:
                                    await self.feishu.send_edit_diff_card(
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
                                    await self._safe_send(message.chat_id, message.message_id, fallback, log_reply=False)
                        elif isinstance(result, list):
                            for marker in result:
                                if isinstance(marker, _DiffMarker):
                                    for card in marker.card if isinstance(marker.card, list) else [marker.card]:
                                        try:
                                            await self.feishu.send_edit_diff_card(
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
                                            await self._safe_send(message.chat_id, message.message_id, fallback, log_reply=False)
                        elif isinstance(result, _MemoryCardMarker):
                            # 记忆工具 → Feishu Interactive Card（按 card_type 渲染）
                            card_md = self._render_memory_card(result)
                            try:
                                await self.feishu.send_interactive_reply(
                                    message.chat_id,
                                    self.formatter.format_text(card_md),
                                    message.message_id,
                                    log_reply=False,
                                )
                            except Exception:
                                logger.warning(f"send_interactive_reply failed for memory tool, falling back")
                                await self._safe_send(message.chat_id, message.message_id, card_md, log_reply=False)
                        elif isinstance(result, _AskUserQuestionMarker):
                            # AskUserQuestion → 精美飞书问卷卡片
                            if result.data is not None:
                                card = format_questionnaire_card(result)
                                try:
                                    await self.feishu.send_edit_diff_card(
                                        message.chat_id, card, message.message_id, log_reply=False
                                    )
                                except Exception as e:
                                    logger.warning(f"send_edit_diff_card failed for AskUserQuestion: {e}, falling back")
                                    await self._safe_send(
                                        message.chat_id, message.message_id,
                                        f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                        log_reply=False,
                                    )
                            else:
                                await self._safe_send(
                                    message.chat_id, message.message_id,
                                    f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                    log_reply=False,
                                )
                        else:
                            await self._safe_send(message.chat_id, message.message_id, result, log_reply=False)
                    elif claude_msg.content:
                        logger.info(f"[stream] text: {claude_msg.content[:100]}")
                        await accumulator.add_text(claude_msg.content)

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
                await self._safe_send(
                    message.chat_id, message.message_id,
                    "⚠️ 查询失败：SDK 返回空响应，请稍后重试。"
                )
                return

            # Save session
            if not session:
                session = self.sessions.create_session(
                    message.user_open_id,
                    self.approved_directory,
                    sdk_session_id=sdk_session_id_from_query,
                    chat_id=message.chat_id,
                )
            else:
                self.sessions.update_session(session.session_id, cost=last_cost, message_increment=1, update_last_message=True)

            # 检测 sdk_session_id 变化，通知用户（strip 消除隐藏字符；排除首次 None->有值的情况）
            new_sid = (sdk_session_id_from_query or "").strip()
            old_sid = (session.sdk_session_id or "").strip()
            if new_sid and old_sid and new_sid != old_sid:
                logger.info(f"[_run_query] sdk_session_id changed: {session.sdk_session_id!r} -> {sdk_session_id_from_query!r}")
                self.sessions.update_sdk_session_id(session.session_id, new_sid)
                await self._safe_send(
                    message.chat_id, message.message_id,
                    f"🔄 检测到新 Session，已自动切换\n新 Session ID: `{new_sid}`",
                    log_reply=False,
                )

            # Send final text response as a Feishu card if no text was streamed.
            # If text was streamed in real-time, it is already visible and not sent again.
            if not accumulator.sent_something:
                if response:
                    from supercc.adapter.feishu.format.agent_card import (
                        format_agent_card,
                        should_use_agent_card,
                    )
                    if should_use_agent_card(response):
                        card = format_agent_card(response)
                        try:
                            await self.feishu.send_card(message.chat_id, card)
                        except Exception:
                            # 卡片失败，降级为普通文本
                            formatted = self.formatter.format_text(response)
                            chunks = self.formatter.split_messages(formatted)
                            for chunk in chunks:
                                await self._safe_send(message.chat_id, message.message_id, chunk)
                    else:
                        formatted = self.formatter.format_text(response)
                        chunks = self.formatter.split_messages(formatted)
                        for chunk in chunks:
                            await self._safe_send(message.chat_id, message.message_id, chunk)

        except asyncio.CancelledError:
            await self._safe_send(message.chat_id, message.message_id, "🛑 已打断 Claude。")
        except Exception as e:
            logger.exception(f"Error in _run_query: {e}")
            # CLI 进程异常崩溃，每次 query 内部创建新 client，下一次自动恢复
            logger.warning(f"[_run_query] CLI error: {e}")
            error_msg = f"⚠️ 内部错误：{e}"
            await self._safe_send(message.chat_id, message.message_id, error_msg)
        finally:
            if reaction_id:
                logger.info(f"[typing] off — user={message.user_open_id}, reaction_id={reaction_id!r}")
                try:
                    await self.feishu.remove_typing_reaction(message.message_id, reaction_id)
                except Exception as exc:
                    logger.warning(f"[typing] remove_typing_reaction failed: {exc}")
            # Trigger memory review after [typing] off
            self._trigger_memory_review(message, _last_response)

            # Trigger skill nudge after query completes (not during streaming)
            nudge = self._skill_nudge
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
                            send_to_feishu=lambda cid, text: self._safe_send(cid, message.message_id, text),
                            skills_dir=Path(self.data_dir) / "skills",
                        )
                    )
                except Exception as e:
                    logger.warning(f"[_trigger_skill_review] failed to start: {e}")

    async def _handle_stop(self, message: IncomingMessage) -> HandlerResult:
        """Handle /stop — cancel the current worker task and interrupt Claude."""
        if not self._is_processing:
            await self._safe_send(message.chat_id, message.message_id, "当前没有正在运行的查询。")
            return HandlerResult(success=True)
        # 立即标记为非运行状态，防止重复调用
        self._is_processing = False
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            self._worker_task = None
        self.claude.stop_event.set()
        await self._safe_send(message.chat_id, message.message_id, "🛑 已打断 Claude，当前任务已停止。")
        return HandlerResult(success=True)

    async def _handle_model(self, message: IncomingMessage) -> HandlerResult:
        """处理 /model 命令：显示所有供应商的模型配置（飞书卡片表格）。"""
        from supercc.claude.model_config import get_all_models, ModelEntry
        from supercc.claude.model_providers import PROVIDERS

        models = get_all_models()

        # 建立 base_url -> (model_id, ModelEntry) 反查表
        url_to_model: dict[str, tuple[str, ModelEntry]] = {}
        for mid, mentry in models.items():
            if mentry.env.ANTHROPIC_AUTH_TOKEN and mentry.env.ANTHROPIC_BASE_URL:
                url_to_model[mentry.env.ANTHROPIC_BASE_URL] = (mid, mentry)

        configured = []
        unconfigured = []

        active_id = None
        for mid, mentry in models.items():
            if mentry.env.ANTHROPIC_AUTH_TOKEN:
                if active_id is None:
                    active_id = mid

        for p in PROVIDERS.values():
            matched = None
            if p.base_url and p.base_url in url_to_model:
                matched = url_to_model[p.base_url]
            if not matched:
                for url, (mid, mentry) in url_to_model.items():
                    if p.base_url and url.startswith(p.base_url.rstrip("/") + "/"):
                        matched = (mid, mentry)
                        break

            if matched:
                mid, mentry = matched
                configured.append((
                    p.id,
                    p.name,
                    mentry.env.ANTHROPIC_AUTH_TOKEN or "",
                    mentry.env.ANTHROPIC_MODEL or "—",
                    p.models,
                    mid == active_id,
                ))
            else:
                unconfigured.append((p.id, p.name, p.models))

        def mask_api_key(key: str) -> str:
            if not key:
                return "—"
            if len(key) <= 10:
                return "****"
            return key[:6] + "***" + key[-4:]

        def fmt_models(models: list[str], current: str) -> str:
            """渲染可用模型列表，当前使用的模型加粗。"""
            parts = []
            for m in models:
                if m == current:
                    parts.append(f"**`{m}`**")
                else:
                    parts.append(f"`{m}`")
            return " / ".join(parts)

        table_rows = [
            "| 状态 | 供应商 | 当前模型 | API Key | 所有可用模型 |",
            "|------|--------|---------|---------|------------|",
        ]
        for pid, pname, api_key, model, all_models, is_active in configured:
            mark = "✅" if is_active else "✴️"
            avail = fmt_models(all_models, model)
            table_rows.append(f"| {mark} | **{pname}** | `{model}` | `{mask_api_key(api_key)}` | {avail} |")
        for pid, pname, all_models in unconfigured:
            avail = " / ".join(f"`{m}`" for m in all_models)
            table_rows.append(f"| 📛 | {pname} | — | — | {avail} |")

        table_md = "\n".join(table_rows)

        active_name = "未设置"
        active_model = "—"
        if active_id and active_id in models:
            e = models[active_id]
            active_name = e.name
            active_model = e.env.ANTHROPIC_MODEL or "—"

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "## 🤖 模型配置\n"
                            f"当前使用：**{active_name}**（`{active_model}`）\n\n"
                            f"共 **{len(configured)}** 个供应商已配置，**{len(unconfigured)}** 个未配置。"
                        ),
                    },
                    {"tag": "markdown", "content": table_md},
                    {
                        "tag": "markdown",
                        "content": "---\n💡 如需切换模型或更新配置，直接跟我说即可。"
                    },
                ]
            },
        }

        try:
            await self.feishu.send_card(message.chat_id, card)
        except Exception:
            text = [f"🤖 **模型配置**（当前：{active_name}）\n"]
            for pid, pname, api_key, model, all_models, is_active in configured:
                m = "✅" if is_active else "✴️"
                text.append(f"{m} {pname}: {model} | {mask_api_key(api_key)}")
            for pid, pname, all_models in unconfigured:
                avail = ", ".join(all_models[:4])
                text.append(f"📛 {pname}: {avail}...")
            text.append(f"\n共{len(configured)}个已配置，{len(unconfigured)}个未配置。\n💡 如需切换模型或更新配置，直接跟我说即可。")
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

    async def _safe_send(self, chat_id: str, reply_to_message_id: str, text: str, log_reply: bool = True):
        """Send a markdown message as a threaded Feishu post/card, ignoring errors.

        Uses Interactive Card for content with fenced code blocks or tables,
        falls back to rich text post for plain markdown.
        """
        try:
            # Optimize and decide format
            formatted = self.formatter.format_text(text)
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


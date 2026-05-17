"""Format Claude's Markdown response for Feishu."""
from __future__ import annotations

import json
import re

from supercc.channels.feishu.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker
from supercc.channels.common.format import MemoryCardMarker
from supercc.channels.feishu.format.questionnaire_card import _AskUserQuestionMarker
from supercc.channels.feishu.format.agent_card import FeishuAgentCardMarker, FeishuCodexMarker
from supercc.channels.feishu.format.markdown_util import optimize_markdown_style, _count_tables_outside_code_blocks
from supercc.core.claude.message_context import get_current_bot_id, get_current_user_open_id

FEISHU_MAX_MESSAGE_LENGTH = 4096
# Feishu CardKit limit for markdown tables per card
FEISHU_CARD_TABLE_LIMIT = 230099

def should_use_card(text: str) -> bool:
    """Decide whether to send as Feishu Interactive Card vs post.

    Cards are used for content with fenced code blocks or markdown tables
    (better rendering in a wide-screen card). Falls back to post if there
    are too many tables (CardKit limit).
    """
    table_count = _count_tables_outside_code_blocks(text)
    if table_count > FEISHU_CARD_TABLE_LIMIT:
        return False
    has_code = bool(re.search(r"```[\s\S]*?```", text))
    if has_code:
        return True
    if table_count > 0:
        return True
    return False


class ReplyFormatter:
    def __init__(self):
        self.tool_icons = {
            "Read": "📖",
            "Write": "✏️",
            "Edit": "🔧",
            "Bash": "💻",
            "Glob": "🔍",
            "Grep": "🔎",
            "WebFetch": "🌐",
            "WebSearch": "🌐",
            "Task": "📋",
            "MemorySearch": "🧠",
            "MemoryList": "🧠",
            "MemoryAdd": "🧠",
            "MemoryDelete": "🧠",
            "MemoryClear": "🧠",
            "FeishuSendFile": "📬",
            "Skill": "🧰",
            "SkillSearch": "🎯",
            "SkillInvoke": "🧰",
            "EnterPlanMode": "🎯",
            "ExitPlanMode": "🎯",
            "AskUserQuestion": "🎯",
            "Agent": "🔀",
            "mcp__codex__codex": "⚡",
        }

    def format_text(self, text: str) -> str:
        """Prepare Markdown text for Feishu post/card rendering.

        Strips non-Feishu image URLs (img_xxx keys only) and applies
        Feishu-specific style optimizations (heading levels, table spacing).
        Code blocks and tables are preserved intact.
        """
        if not text:
            return ""
        text = optimize_markdown_style(text, card_version=2)
        return text.strip()

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str | _DiffMarker | list[_DiffMarker] | MemoryCardMarker | _AskUserQuestionMarker | FeishuAgentCardMarker | FeishuCodexMarker:
        """Format a tool call notification for the user.

        Returns _DiffMarker for Edit/Write tools (to trigger colored card rendering),
        or a plain string for all other tools.
        """
        if tool_input is None:
            tool_input = ""

        # Edit / Write → 彩色 diff 卡片
        if tool_name == "Edit":
            if tool_input.strip():
                try:
                    return build_edit_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式
        elif tool_name == "Write":
            if tool_input.strip():
                try:
                    return build_write_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式

        # Bash → md 代码段，description 转注释
        elif tool_name == "Bash":
            return self._format_bash_tool(tool_input)

        # TodoWrite → markdown 表格
        elif tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input)

        # AskUserQuestion → 精美问卷卡片
        elif tool_name == "AskUserQuestion":
            marker = _AskUserQuestionMarker(tool_name, tool_input)
            if marker.data is not None:
                return marker
            # 解析失败，降级为普通文本
            return f"🔀 **{tool_name}**\n`{tool_input}`"

        # Memory MCP tools → 卡片标记（触发 Feishu Interactive Card）
        elif tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            return self._format_memory_tool(
                tool_name, tool_input,
                memory_manager=kwargs.get("memory_manager"),
                default_project_path=kwargs.get("default_project_path", ""),
                platform=kwargs.get("platform", "feishu"),
                chat_id=kwargs.get("chat_id", ""),
            )

        # Cron MCP tools → ⏰ 时钟图标
        elif tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            icon = "⏰"
            msg = f"{icon} **{short_name}**"
            if tool_input and len(tool_input) <= FEISHU_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            return msg

        # 其他 mcp__SuperCC__ 工具（非 Memory/Cron）→ 去掉前缀后走通用逻辑
        elif tool_name and tool_name.startswith("mcp__SuperCC__"):
            tool_name = tool_name.replace("mcp__SuperCC__", "")

        # Read → 提取 file_path，用 backtick 包裹
        elif tool_name == "Read":
            return self._format_read_tool(tool_input)

        # Agent (sub-agent) → 展示 description + 摘要
        elif tool_name == "Agent":
            return self._format_agent_tool(tool_input)

        # mcp__codex__codex → 展示模型 + 摘要
        elif tool_name == "mcp__codex__codex":
            return self._format_codex_tool(tool_input)

        # 其他工具 → backtick 格式（原有逻辑）
        icon = self.tool_icons.get(tool_name, "🤖")
        msg = f"{icon} **{tool_name}**"
        if tool_input:
            if len(tool_input) <= FEISHU_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            else:
                chunks = self.split_messages(tool_input)
                for chunk in chunks:
                    msg += f"\n`{chunk}`"
        return msg

    # ── Memory MCP tool 格式化 ────────────────────────────────────────────────
    _MEM_PAGE_SIZE = 5

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "feishu",
        chat_id: str = "",
        bot_id: str = "",
    ) -> MemoryCardMarker | str:
        """格式化记忆 MCP 工具调用为卡片标记。

        card_type 决定 stream_callback 如何渲染：
        - add     → 参数表格（标题列置顶）
        - update  → 参数表格（标题列置顶）
        - delete  → 删除条目 ID 表格
        - list    → 实际记忆条目表格（查库）
        - search  → 搜索匹配条目表格（查库）
        """
        try:
            args = json.loads(tool_input) if tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = tool_name.replace("mcp__SuperCC__", "")
        # MemoryAddProj → add_proj → card_type="add", scope="proj"
        # MemoryListUser → list_user → card_type="list", scope="user"
        parts = re.split(r"(?=[A-Z])", short.replace("Memory", ""))
        card_type_map = {
            "Add": "add", "Delete": "delete",
            "Update": "update", "List": "list", "Search": "search",
        }
        card_type = None
        scope = ""
        for p in parts:
            if p in card_type_map:
                card_type = card_type_map[p]
            else:
                scope = p.lower()   # Proj → proj, User → user

        # ── 根据操作类型查库获取真实 entries ───────────────────────────────
        entries = []

        if card_type in ("list", "search"):
            # list/search — 查询实际条目（project_path 缺失时使用 default_project_path）
            query = args.get("query", "")
            project_path = args.get("project_path", "") or default_project_path
            user_open_id = args.get("user_open_id", "") or get_current_user_open_id() or ""
            bot_id = bot_id or args.get("bot_id", "") or get_current_bot_id() or ""

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
                            entries = []
                    else:
                        # user scope — 必须提供 user_open_id，否则无法确定归属
                        if user_open_id:
                            prefs = memory_manager.get_preferences_by_user(user_open_id, platform=platform, bot_id=bot_id)
                            if card_type == "search" and query:
                                prefs = memory_manager.search_preferences(query, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
                        else:
                            # user_open_id 为空时不能返回所有用户偏好（隐私泄漏），返回空列表
                            entries = []
                except Exception:
                    entries = []

        elif card_type == "delete":
            # delete — 直接用 tool_input 里的 id（CC 先删 DB，无法再查）
            mem_id = args.get("id", "")
            if mem_id:
                entries = [{"id": mem_id}]

        # add / update — entries 就是当前操作的参数
        if not entries and card_type in ("add", "update"):
            entries = [{"title": args.get("title", ""),
                        "content": args.get("content", ""),
                        "keywords": args.get("keywords", ""),
                        "id": args.get("id", "") or "(新增)"}]

        return MemoryCardMarker(tool_name, card_type, entries, tool_input)

    def _format_bash_tool(self, tool_input: str) -> str:
        """Format Bash tool call as a markdown code block.

        If description exists, append it to the header line.
        Code block only contains the command.
        """
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            # 不是合法 JSON，降级
            return f"💻 **Bash**\n```bash\n{tool_input}\n```"

        command = data.get("command", tool_input)
        description = data.get("description")

        icon = self.tool_icons.get("Bash", "💻")
        if description:
            header = f"{icon} **Bash** — {description}"
        else:
            header = f"{icon} **Bash**"

        return f"{header}\n```bash\n{command}\n```"

    def _format_read_tool(self, tool_input: str) -> str:
        """Format Read tool call with backtick-wrapped file path and optional offset/limit."""
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
            file_path = data.get("file_path", tool_input)
        except (json.JSONDecodeError, TypeError):
            file_path = tool_input
            data = {}

        icon = self.tool_icons.get("Read", "📖")
        extras = []
        if data.get("offset") is not None:
            extras.append(f"offset {data['offset']}")
        if data.get("limit") is not None:
            extras.append(f"limit {data['limit']}")

        if extras:
            title = f"**Read** — " + " — ".join(extras)
            path_line = f"`{file_path}`"
        else:
            title = f"**Read**"
            path_line = f"`{file_path}`"
        return f"{icon} {title}\n{path_line}"

    def _format_agent_tool(self, tool_input: str) -> FeishuAgentCardMarker:
        """Format Agent (sub-agent) tool call → FeishuAgentCardMarker。"""
        return FeishuAgentCardMarker("Agent", tool_input)

    def _format_codex_tool(self, tool_input: str) -> FeishuCodexMarker:
        """Format mcp__codex__codex tool call → FeishuCodexMarker。"""
        try:
            data = json.loads(tool_input) if tool_input else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        prompt = data.get("prompt", tool_input or "")
        model = data.get("model", "")
        event_type = "text"
        extra = {"model": model} if model else None
        return FeishuCodexMarker(event_type, prompt, extra, tool_input)


    def _format_todowrite_tool(self, tool_input: str) -> str:
        """Format TodoWrite tool call as a markdown table."""
        try:
            data = json.loads(tool_input)
            todos = data.get("todos", [])
        except json.JSONDecodeError:
            todos = []

        if not isinstance(todos, list):
            todos = []

        if not todos:
            return "✅ 所有任务已完成！"

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        rows = ["| 状态 | 待办事项 | 当前动作 |", "|------|----------|----------|"]
        for t in todos:
            icon = status_icon.get(t.get("status", "pending"), "⬜")
            content = str(t.get("content", "")).replace("\n", " ").replace("|", "\\|")
            active = str(t.get("activeForm", "")).replace("\n", " ").replace("|", "\\|")
            rows.append(f"| {icon} | {content} | {active} |")

        return "📋 Todo List\n\n" + "\n".join(rows)

    def should_use_card(self, text: str) -> bool:
        """Decide whether to send as Feishu Interactive Card vs post.

        Delegates to the module-level should_use_card for the actual logic.
        """
        return should_use_card(text)

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under Feishu's limit."""
        if len(text) <= FEISHU_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= FEISHU_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                # If single line exceeds limit, split by chars
                if len(line) > FEISHU_MAX_MESSAGE_LENGTH:
                    while len(line) > FEISHU_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:FEISHU_MAX_MESSAGE_LENGTH])
                        line = line[FEISHU_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]
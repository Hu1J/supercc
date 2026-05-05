"""Format Claude's Markdown response for WeCom."""
from __future__ import annotations

import json
import re

from supercc.adapter.wecom.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker, _MemoryCardMarker
from supercc.adapter.wecom.format.questionnaire_card import _AskUserQuestionMarker
from supercc.claude.message_context import get_current_user_open_id

WECOM_MAX_MESSAGE_LENGTH = 4096


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
            "SkillSearch": "🎯",
            "EnterPlanMode": "🎯",
            "ExitPlanMode": "🎯",
            "AskUserQuestion": "🎯",
        }

    def format_text(self, text: str) -> str:
        """Prepare Markdown text for WeCom rendering."""
        if not text:
            return ""
        return text.strip()

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str | _DiffMarker | list[_DiffMarker] | _MemoryCardMarker:
        """Format a tool call notification for the user."""
        if tool_input is None:
            tool_input = ""

        if tool_name == "Edit":
            if tool_input.strip():
                try:
                    return build_edit_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass
        elif tool_name == "Write":
            if tool_input.strip():
                try:
                    return build_write_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass
        elif tool_name == "Bash":
            return self._format_bash_tool(tool_input)
        elif tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input)
        elif tool_name == "AskUserQuestion":
            marker = _AskUserQuestionMarker(tool_name, tool_input)
            if marker.data is not None:
                return marker
            return f"🤖 **{tool_name}**\n`{tool_input}`"
        elif tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            return self._format_memory_tool(
                tool_name, tool_input,
                memory_manager=kwargs.get("memory_manager"),
                default_project_path=kwargs.get("default_project_path", ""),
                platform=kwargs.get("platform", "wecom"),
                chat_id=kwargs.get("chat_id", ""),
            )
        elif tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            icon = "⏰"
            msg = f"{icon} **{short_name}**"
            if tool_input and len(tool_input) <= WECOM_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            return msg
        elif tool_name and tool_name.startswith("mcp__SuperCC__"):
            tool_name = tool_name.replace("mcp__SuperCC__", "")
        elif tool_name == "Read":
            return self._format_read_tool(tool_input)

        icon = self.tool_icons.get(tool_name, "🤖")
        msg = f"{icon} **{tool_name}**"
        if tool_input:
            if len(tool_input) <= WECOM_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            else:
                chunks = self.split_messages(tool_input)
                for chunk in chunks:
                    msg += f"\n`{chunk}`"
        return msg

    _MEM_PAGE_SIZE = 5

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "wecom",
        chat_id: str = "",
    ) -> _MemoryCardMarker | str:
        try:
            args = json.loads(tool_input) if tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = tool_name.replace("mcp__SuperCC__", "")
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
                scope = p.lower()

        entries = []
        if card_type in ("list", "search"):
            query = args.get("query", "")
            project_path = args.get("project_path", "") or default_project_path
            user_open_id = args.get("user_open_id", "") or get_current_user_open_id() or ""

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
                        if user_open_id:
                            prefs = memory_manager.get_preferences_by_user(user_open_id, platform=platform)
                            if card_type == "search" and query:
                                prefs = memory_manager.search_preferences(query, user_open_id=user_open_id, platform=platform)
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
                except Exception:
                    entries = []

        elif card_type == "delete":
            mem_id = args.get("id", "")
            if mem_id:
                entries = [{"id": mem_id}]

        if not entries and card_type in ("add", "update"):
            entries = [{"title": args.get("title", ""),
                        "content": args.get("content", ""),
                        "keywords": args.get("keywords", ""),
                        "id": args.get("id", "") or "(新增)"}]

        return _MemoryCardMarker(tool_name, card_type, entries, tool_input)

    def _format_bash_tool(self, tool_input: str) -> str:
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
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
        else:
            title = f"**Read**"
        return f"{icon} {title}\n`{file_path}`"

    def _format_todowrite_tool(self, tool_input: str) -> str:
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
        """WeCom does not have interactive cards like Feishu; always return False."""
        return False

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under WeCom's limit."""
        if len(text) <= WECOM_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= WECOM_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                if len(line) > WECOM_MAX_MESSAGE_LENGTH:
                    while len(line) > WECOM_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:WECOM_MAX_MESSAGE_LENGTH])
                        line = line[WECOM_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]

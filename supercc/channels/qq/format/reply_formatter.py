"""QQ channel reply formatter.

QQ has limited formatting support:
- Plain text (recommended)
- Basic markdown (msg_type=2): **bold**, `code`, ```code blocks```

No native support for:
- Tables (rendered as plain text)
- Cards/buttons
- Rich interactive elements
"""
from __future__ import annotations

import json
import re

from supercc.channels.common.format import MemoryCardMarker

QQ_MAX_MESSAGE_LENGTH = 4000


class QQReplyFormatter:
    """Format tool call results and AI responses for QQ (limited markdown support)."""

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

    def format_text(self, text: str) -> str:
        """Prepare text for QQ sending (strips unsupported markdown, truncates if needed)."""
        if not text:
            return ""
        # Strip unsupported elements that might cause rendering issues
        text = text.strip()
        if len(text) > QQ_MAX_MESSAGE_LENGTH:
            text = text[:QQ_MAX_MESSAGE_LENGTH - 3] + "..."
        return text

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str | MemoryCardMarker:
        """Format a tool call notification as QQ-friendly text or MemoryCardMarker."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "").replace("mcp__codex__", "")

        # Edit → diff (simplified for QQ)
        if tool_name == "Edit":
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                return f"{icon} **{short_name}** — `{file_path}`"
            except (json.JSONDecodeError, KeyError):
                return f"{icon} **{short_name}**"

        # Write → file path
        if tool_name == "Write":
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                return f"{icon} **{short_name}** — `{file_path}`"
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
                return f"{header}\n```\n{cmd}\n```"
            except json.JSONDecodeError:
                return f"{icon} **Bash**\n```\n{tool_input}\n```"

        # TodoWrite → markdown table (simplified)
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

        # Memory MCP tools → MemoryCardMarker
        if tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            return self._format_memory_tool(tool_name, tool_input, **kwargs)

        # Cron MCP tools → ⏰
        if tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            return f"⏰ **{short_name}**"

        # Default: icon + name + first 100 chars
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        memory_manager=None,
        default_project_path: str = "",
        platform: str = "qq",
        chat_id: str = "",
        **kwargs,
    ) -> MemoryCardMarker | str:
        """Format memory MCP tool call as MemoryCardMarker."""
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
                    user_open_id = args.get("user_open_id", "")
                    if user_open_id:
                        if card_type == "list":
                            prefs = memory_manager.get_preferences_by_user(user_open_id, platform=platform, bot_id="")
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
                        elif card_type == "search" and query:
                            prefs = memory_manager.search_preferences(query, user_open_id=user_open_id, platform=platform, bot_id="")
                            entries = [{"id": p.id, "title": p.title,
                                        "content": p.content, "keywords": p.keywords} for p in prefs]
            except Exception:
                pass

        # add/update fallback
        if not entries and card_type in ("add", "update"):
            entries = [{
                "title": args.get("title", ""),
                "content": args.get("content", ""),
                "keywords": args.get("keywords", ""),
                "id": args.get("id", "") or "(新增)"}]
        elif not entries and card_type == "delete":
            entries = [{"id": args.get("id", "") or ""}]

        return MemoryCardMarker(tool_name, card_type, entries, tool_input)

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under QQ's limit."""
        if len(text) <= QQ_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= QQ_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                if len(line) > QQ_MAX_MESSAGE_LENGTH:
                    while len(line) > QQ_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:QQ_MAX_MESSAGE_LENGTH])
                        line = line[QQ_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]
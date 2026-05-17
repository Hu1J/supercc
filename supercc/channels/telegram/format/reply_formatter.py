"""Format Claude's response for Telegram (MarkdownV2 with emoji diff)."""
from __future__ import annotations

import json
import re

from supercc.channels.common.format.diff import colorize_diff, DiffLine


def format_edit_markdown(file_path: str, diff_lines: list) -> str:
    """Format Edit tool result as emoji-based diff for Telegram.

    Telegram has no native card support, so we render diff as:
    - Context lines: plain text
    - Deletions: 🔴 -line (red emoji prefix)
    - Insertions: 🟢 +line (green emoji prefix)
    """
    if not diff_lines:
        return f"✏️ **Edit** — `{file_path}`"

    lines = []
    for dl in diff_lines:
        if dl.type == "deletion":
            lines.append(f"🔴 -{dl.content}")
        elif dl.type == "insertion":
            lines.append(f"🟢 +{dl.content}")
        else:
            lines.append(f"  {dl.content}")

    diff_text = "\n".join(lines)
    return f"✏️ **Edit** — `{file_path}`\n\n{diff_text}"


def format_write_markdown(file_path: str, content: list[str]) -> str:
    """Format Write tool result as emoji diff for Telegram.

    Shows new file content as 🟢 +line for each line.
    """
    if not content:
        return f"✏️ **Write** — `{file_path}` (empty file)"

    lines = []
    for line in content:
        lines.append(f"🟢 +{line}")

    diff_text = "\n".join(lines)
    return f"✏️ **Write** — `{file_path}`\n\n{diff_text}"


class ReplyFormatter:
    """Format tool call results for Telegram."""

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
            "TodoWrite": "📋",
            "MemorySearch": "🧠",
            "MemoryList": "🧠",
            "MemoryAdd": "🧠",
            "MemoryDelete": "🧠",
            "MemoryClear": "🧠",
            "AskUserQuestion": "🎯",
            "SkillSearch": "🎯",
            "SkillInvoke": "🧰",
            "Agent": "🔀",
        }

    def format_text(self, text: str) -> str:
        """Prepare text for Telegram MarkdownV2.

        Since Telegram MarkdownV2 requires escaping many characters,
        we use a simpler approach - escape problematic characters.
        """
        if not text:
            return ""
        return text.strip()

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str:
        """Format a tool call notification for Telegram.

        Returns a string representation since Telegram has no native cards.
        """
        if tool_input is None:
            tool_input = ""

        icon = self.tool_icons.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit → emoji diff
        if tool_name == "Edit":
            try:
                data = json.loads(tool_input)
                file_path = data.get("file_path", "unknown")
                old_string = data.get("old_string", "")
                new_string = data.get("new_string", "")
                diff_lines = colorize_diff(old_string, new_string)
                return format_edit_markdown(file_path, diff_lines)
            except (json.JSONDecodeError, KeyError):
                return f"{icon} **{short_name}**"

        # Write → emoji diff
        if tool_name == "Write":
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

        # Cron MCP tools → ⏰ icon
        if tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            icon = "⏰"
            msg = f"{icon} **{short_name}**"
            if tool_input and len(tool_input) <= 200:
                msg += f"\n`{tool_input[:100]}`"
            return msg

        # Memory MCP tools → 🧠 icon
        if tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            icon = "🧠"
            msg = f"{icon} **{short_name}**"
            if tool_input and len(tool_input) <= 200:
                msg += f"\n`{tool_input[:100]}`"
            return msg

        # Default: icon + name + input preview
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg
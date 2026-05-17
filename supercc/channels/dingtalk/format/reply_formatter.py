"""Format Claude's Markdown response for DingTalk."""
from __future__ import annotations

import json
import re

DINGTALK_MAX_MESSAGE_LENGTH = 20000


class ReplyFormatter:
    """Format tool call results for DingTalk (limited markdown support)."""

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
        "MemoryUpdate": "🧠",
        "AskUserQuestion": "🎯",
        "SkillSearch": "🎯",
        "SkillInvoke": "🎯",
        "CronCreate": "⏰",
        "CronDelete": "⏰",
        "CronList": "⏰",
        "CronPause": "⏰",
        "CronResume": "⏰",
        "CronTrigger": "⏰",
        "CronLogs": "⏰",
        "Agent": "🔀",
        "mcp__codex__codex": "⚡",
    }

    def format_text(self, text: str) -> str:
        """Prepare Markdown text for DingTalk rendering."""
        if not text:
            return ""
        return self._normalize_markdown(text.strip())

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str:
        """Format a tool call notification for the user."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "")

        # Edit → file path
        if tool_name == "Edit":
            return self._format_edit_tool(tool_input, icon)

        # Write → file path
        if tool_name == "Write":
            return self._format_write_tool(tool_input, icon)

        # Bash → code block
        if tool_name == "Bash":
            return self._format_bash_tool(tool_input, icon)

        # TodoWrite → markdown table
        if tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input, icon)

        # Read → file path
        if tool_name == "Read":
            return self._format_read_tool(tool_input, icon)

        # Agent → description
        if tool_name == "Agent":
            return self._format_agent_tool(tool_input, icon)

        # mcp__codex__codex → model + content
        if tool_name == "mcp__codex__codex":
            return self._format_codex_tool(tool_input, icon)

        # Memory MCP tools → simple text
        if tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            return self._format_memory_tool(tool_name, tool_input, icon)

        # Cron MCP tools → ⏰
        if tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            return self._format_cron_tool(tool_name, tool_input, icon)

        # Default: icon + name + first 100 chars of input
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    def _format_edit_tool(self, tool_input: str, icon: str) -> str:
        """Format Edit tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            file_path = data.get("file_path", "unknown")
            return f"{icon} **Edit** — `{file_path}`"
        except json.JSONDecodeError:
            return f"{icon} **Edit**"

    def _format_write_tool(self, tool_input: str, icon: str) -> str:
        """Format Write tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            file_path = data.get("file_path", "unknown")
            return f"{icon} **Write** — `{file_path}`"
        except json.JSONDecodeError:
            return f"{icon} **Write**"

    def _format_bash_tool(self, tool_input: str, icon: str) -> str:
        """Format Bash tool call as a markdown code block."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            cmd = data.get("command", tool_input)
            desc = data.get("description", "")
            header = f"{icon} **Bash**"
            if desc:
                header += f" — {desc}"
            return f"{header}\n```bash\n{cmd}\n```"
        except json.JSONDecodeError:
            return f"{icon} **Bash**\n```bash\n{tool_input}\n```"

    def _format_todowrite_tool(self, tool_input: str, icon: str) -> str:
        """Format TodoWrite tool call as a markdown table."""
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

    def _format_read_tool(self, tool_input: str, icon: str) -> str:
        """Format Read tool call with backtick-wrapped file path."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            path = data.get("file_path", tool_input)
        except json.JSONDecodeError:
            path = tool_input
        return f"{icon} **Read** — `{path}`"

    def _format_agent_tool(self, tool_input: str, icon: str) -> str:
        """Format Agent tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            desc = data.get("description", "sub-agent")
            return f"{icon} **Agent** — {desc[:100]}"
        except json.JSONDecodeError:
            return f"{icon} **Agent**"

    def _format_codex_tool(self, tool_input: str, icon: str) -> str:
        """Format mcp__codex__codex tool call."""
        try:
            data = json.loads(tool_input) if tool_input else {}
            event_type = data.get("event_type", "text")
            content = data.get("content", tool_input or "")
            model = data.get("model", "")
            header = f"{icon} **Codex**"
            if model:
                header += f" ({model})"
            return f"{header}\n{content[:200]}"
        except json.JSONDecodeError:
            return f"{icon} **Codex**"

    def _format_memory_tool(self, tool_name: str, tool_input: str, icon: str) -> str:
        """Format Memory MCP tool call as simple text."""
        short = tool_name.replace("mcp__SuperCC__", "")
        msg = f"{icon} **{short}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    def _format_cron_tool(self, tool_name: str, tool_input: str, icon: str) -> str:
        """Format Cron MCP tool call."""
        short = tool_name.replace("mcp__SuperCC__", "")
        msg = f"{icon} **{short}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        """Normalize markdown for DingTalk's parser.

        DingTalk's markdown renderer has quirks:
        - Numbered lists need blank line before them
        - Indented code blocks may render incorrectly
        """
        lines = text.split("\n")
        out = []
        for i, line in enumerate(lines):
            # Ensure blank line before numbered list items
            is_numbered = re.match(r"^\d+\.\s", line.strip())
            if is_numbered and i > 0:
                prev = lines[i - 1]
                if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                    out.append("")
            # Dedent fenced code blocks
            if line.strip().startswith("```") and line != line.lstrip():
                indent = len(line) - len(line.lstrip())
                line = line[indent:]
            out.append(line)
        return "\n".join(out)

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under DingTalk's limit."""
        if len(text) <= DINGTALK_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= DINGTALK_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                if len(line) > DINGTALK_MAX_MESSAGE_LENGTH:
                    while len(line) > DINGTALK_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:DINGTALK_MAX_MESSAGE_LENGTH])
                        line = line[DINGTALK_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]

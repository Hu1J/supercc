"""微信个人消息格式化工具。

用于将工具调用结果渲染为微信友好的 Markdown 格式。
微信支持有限 Markdown（bold/code），不支持复杂卡片。
"""
from __future__ import annotations

import json
from typing import Optional


class WeChatReplyFormatter:
    """微信消息格式化器。"""

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

    def format_tool_call(self, tool_name: str, tool_input: Optional[str] = None) -> str:
        """Format a tool call notification as WeChat-friendly text."""
        if tool_input is None:
            tool_input = ""

        icon = self.ICONS.get(tool_name, "🤖")
        short_name = tool_name.replace("mcp__SuperCC__", "").replace("mcp__", "")

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

        # Default: icon + name
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= 200:
            msg += f"\n`{tool_input[:100]}`"
        elif tool_input:
            msg += f"\n`{tool_input[:100]}...`"
        return msg

    def format_message(self, content: str) -> str:
        """Format general message content for WeChat.

        WeChat supports limited Markdown (bold, code), so we do light normalization.
        """
        if not content:
            return content

        # Wrap long lines for better chat readability (WeChat has no native wrap)
        import textwrap
        lines = content.splitlines()
        wrapped_lines = []
        for line in lines:
            if len(line) <= 120:
                wrapped_lines.append(line)
            else:
                wrapped = textwrap.wrap(line, width=120, break_long_words=False, break_on_hyphens=False)
                wrapped_lines.extend(wrapped or [line])
        return "\n".join(wrapped_lines)


def format_tool_call(tool_name: str, tool_input: Optional[str] = None) -> str:
    """Convenience function for formatting tool calls."""
    formatter = WeChatReplyFormatter()
    return formatter.format_tool_call(tool_name, tool_input)
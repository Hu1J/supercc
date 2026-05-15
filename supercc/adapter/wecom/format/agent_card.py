"""Agent 响应渲染 — WeCom markdown 版本。"""
from __future__ import annotations

import json


def format_agent_markdown(text: str | dict, title: str = "🤖 Agent") -> str:
    """将 Agent 响应格式化为 WeCom markdown。"""
    data = None
    if isinstance(text, dict):
        data = text
    elif text and text.strip().startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

    if data and isinstance(data, dict):
        parts = [f"**{title}**"]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---\n")
        return "\n".join(parts)
    else:
        return f"**{title}**\n\n{text or ''}"


def format_codex_markdown(event_type: str, content: str = "") -> str:
    """将 Codex 事件格式化为 WeCom markdown。"""
    icons = {
        "text": "🧩",
        "tool_use": "⚙️",
        "command_execution": "💻",
        "command_output": "📤",
        "file_change": "📝",
        "reasoning": "🧠",
        "todo_list": "☑️",
        "error": "⚠️",
        "finished": "✅",
        "started": "🚀",
    }
    icon = icons.get(event_type, "🤖")
    return f"**{icon} Codex - {event_type}**\n\n{content}"

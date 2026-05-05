"""Agent / Codex 响应卡片 — WeCom 使用 markdown 格式渲染."""
from __future__ import annotations

import json


def _codex_event_label(event_type: str, extra: dict | None = None) -> str:
    tool_name = (extra or {}).get("tool_name") or ""
    tool_icons = {
        "Read": "📖",
        "Write": "✏️",
        "Edit": "🔧",
        "Bash": "💻",
        "exec_command": "💻",
        "command_execution": "💻",
        "Grep": "🔎",
        "Glob": "🔍",
        "WebFetch": "🌐",
        "WebSearch": "🌐",
    }
    if event_type == "text":
        return "🧩 content"
    if event_type == "tool_use":
        icon = tool_icons.get(tool_name, "⚙️")
        return f"{icon} {tool_name}" if tool_name else "⚙️ tool"
    if event_type == "command_execution":
        return "💻 command"
    if event_type == "command_output":
        return "📤 output"
    if event_type == "file_change":
        return "📝 file"
    if event_type == "reasoning":
        return "🧠 reasoning"
    if event_type == "todo_list":
        return "☑️ todo"
    if event_type == "error":
        return "⚠️ error"
    if event_type == "finished":
        return "✅ finished"
    if event_type == "started":
        return "🚀 started"
    return event_type or "event"


def format_codex_card(event_type: str, content: str = "", extra: dict | None = None) -> dict:
    """构建 Codex 事件 markdown 卡片（返回 dict 供 template_card 使用，或直接用 markdown）。"""
    title = f"## 🤖 Codex - {_codex_event_label(event_type, extra)}"
    body = content or ""
    return {
        "type": "markdown",
        "content": f"{title}\n\n{body}" if body else title,
    }


def format_agent_card(text: str | dict, title: str = "## 🤖 Agent") -> dict:
    """构建 Agent / Plan 响应 markdown 卡片。

    WeCom 不支持飞书 CardKit，直接输出 markdown 文本。
    返回 dict 以便 message_handler 统一处理。
    """
    data = None
    if isinstance(text, dict):
        data = text
    elif text and text.strip().startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

    if data and isinstance(data, dict):
        parts = [title]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("")
        content = "\n".join(parts)
    else:
        content = f"{title}\n\n{text or ''}"

    return {
        "type": "markdown",
        "content": content.strip(),
    }

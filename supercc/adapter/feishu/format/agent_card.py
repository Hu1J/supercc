"""Agent 响应飞书卡片 — 将 Claude 的最终响应渲染为精美的 Interactive Card。"""
from __future__ import annotations

import json

from supercc.adapter.feishu.format.reply_formatter import optimize_markdown_style


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
    """构建 Codex 事件飞书卡片。

    统一标题格式：`## 🤖 Codex - <label>`，正文直接展示内容，不再包一层
    `content:` / `tool:` 键值文本。
    """
    title = f"## 🤖 Codex - {_codex_event_label(event_type, extra)}"
    body = optimize_markdown_style(content or "", card_version=2)
    content = title if not body else f"{title}\n\n{body}"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                },
            ]
        },
    }


def format_agent_card(text: str | dict, title: str = "## 🤖 Agent") -> dict:
    """构建 Agent / Plan 响应飞书卡片。

    tool_input 为 dict 时直接解析；为 JSON 字符串时解析为 key-value 对，
    字段间用 `--` 分割；非 JSON 时直接渲染为 markdown。
    """
    # 尝试解析 JSON
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
            parts.append("\n---\n")
        content = "\n".join(parts)
    else:
        # 非 JSON：直接走 markdown 优化
        content = optimize_markdown_style(text or "", card_version=2)
        content = f"{title}\n\n{content}"

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                },
            ]
        },
    }

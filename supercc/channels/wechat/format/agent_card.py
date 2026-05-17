"""Agent / Codex 响应 WeChat 格式化 — 纯文本 markdown 版本。
WeChat 不支持 CardKit，使用 markdown 格式输出。
"""
from __future__ import annotations
import json

from supercc.channels.common.format.agent import _AgentCardMarker, _CodexMarker
from supercc.channels.wechat.format.markdown_util import optimize_markdown_style


class WeChatAgentCardMarker(_AgentCardMarker):
    """Agent 卡片 — WeChat 平台输出纯 markdown。"""

    def render(self) -> str:
        """构建 Agent markdown 文本。"""
        data = self.data
        if data and isinstance(data, dict):
            parts = ["**🔀 Agent**"]
            for key, value in data.items():
                parts.append(f"**{key}**: {value}")
                parts.append("\n---")
            return "\n".join(parts)
        content = optimize_markdown_style(self.tool_input or "", card_version=2)
        return f"**🔀 Agent**\n\n{content}"


class WeChatCodexMarker(_CodexMarker):
    """Codex 卡片 — WeChat 平台输出纯 markdown。"""

    def render(self) -> str:
        """构建 Codex 事件 markdown 文本。"""
        icon = self.ICONS.get(self.event_type, "🔀")
        label = self._event_label()
        body = optimize_markdown_style(self.content or "", card_version=2)
        title = f"**{icon} Codex - {label}**"
        return title if not body else f"{title}\n\n{body}"


def _codex_event_label(event_type: str, extra: dict | None = None) -> str:
    """Codex 事件标签（复用于 format_codex_card）。"""
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
        return "content"
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


def format_agent_card(text: str | dict, title: str = "**🔀 Agent**") -> str:
    """构建 Agent markdown 文本（供 core_client.py 直接调用）。"""
    if isinstance(text, dict):
        data = text
    elif text and text.strip().startswith("{"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
    else:
        data = None

    if data and isinstance(data, dict):
        parts = [title]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---")
        return "\n".join(parts)
    content = optimize_markdown_style(text or "", card_version=2)
    return f"{title}\n\n{content}"


def format_codex_card(event_type: str, content: str = "", extra: dict | None = None) -> str:
    """构建 Codex 事件 markdown 文本（供 core_client.py 直接调用）。"""
    icon = _codex_event_label(event_type, extra)
    body = optimize_markdown_style(content or "", card_version=2)
    title = f"**🔀 Codex - {icon}**"
    return title if not body else f"{title}\n\n{body}"
"""Agent / Codex 响应 WhatsApp 格式 — 纯文本渲染。

WhatsApp 不支持卡片格式，直接使用平台无关的纯文本渲染。
"""
from __future__ import annotations

from supercc.channels.common.format.agent import _AgentCardMarker, _CodexMarker
from supercc.channels.whatsapp.format.markdown_util import optimize_markdown_style


class WhatsAppAgentCardMarker(_AgentCardMarker):
    """Agent 卡片 — WhatsApp 平台直接使用父类纯文本渲染。"""

    def render(self) -> str:
        """构建 Agent WhatsApp 纯文本消息。"""
        data = self.data
        if data and isinstance(data, dict):
            parts = ["🔀 **Agent**"]
            for key, value in data.items():
                parts.append(f"**{key}**: {value}")
                parts.append("\n---\n")
            content = "\n".join(parts)
        else:
            content = optimize_markdown_style(self.tool_input or "")
            content = f"🔀 **Agent**\n\n{content}"
        return content


class WhatsAppCodexMarker(_CodexMarker):
    """Codex 卡片 — WhatsApp 平台直接使用父类纯文本渲染。"""

    def render(self) -> str:
        """构建 Codex 事件 WhatsApp 纯文本消息。"""
        icon = self.ICONS.get(self.event_type, "🔀")
        label = self._event_label()
        body = optimize_markdown_style(self.content or "")
        return f"🔀 **{icon} Codex - {label}**\n\n{body}"


def format_agent_card(text: str | dict, title: str = "🔀 **Agent**") -> str:
    """构建 Agent WhatsApp 纯文本消息（供 core_client.py 直接调用）。"""
    if isinstance(text, dict):
        data = text
    elif text and text.strip().startswith("{"):
        try:
            import json
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
    else:
        data = None

    if data and isinstance(data, dict):
        parts = [title]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---\n")
        content = "\n".join(parts)
    else:
        content = optimize_markdown_style(text or "")
        content = f"{title}\n\n{content}"
    return content


def format_codex_card(event_type: str, content: str = "", extra: dict | None = None) -> str:
    """构建 Codex 事件 WhatsApp 纯文本消息（供 core_client.py 直接调用）。"""
    ICONS = {
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
    icon = ICONS.get(event_type, "🔀")
    label = event_type or "event"
    body = optimize_markdown_style(content or "")
    return f"🔀 **{icon} Codex - {label}**\n\n{body}"
"""Agent / Codex 响应渲染 — Telegram 平台。

Telegram 不支持 CardKit，使用平台无关的纯 markdown 格式。
"""
from __future__ import annotations

from supercc.channels.common.format.agent import _AgentCardMarker, _CodexMarker, build_codex_marker


class TelegramAgentCardMarker(_AgentCardMarker):
    """Agent 卡片 — Telegram 平台使用纯 markdown。"""

    def render(self) -> str:
        """构建 Agent Telegram 文本（纯 markdown）。"""
        data = self.data
        if data and isinstance(data, dict):
            parts = ["**🔀 Agent**"]
            for key, value in data.items():
                parts.append(f"**{key}**: {value}")
                parts.append("\n---\n")
            return "\n".join(parts)
        return f"**🔀 Agent**\n\n{self.tool_input or ''}"


class TelegramCodexMarker(_CodexMarker):
    """Codex 事件卡片 — Telegram 平台使用纯 markdown。"""

    def render(self) -> str:
        """构建 Codex 事件 Telegram 文本（纯 markdown）。"""
        icon = self.ICONS.get(self.event_type, "🔀")
        label = self._event_label()
        return f"**{icon} Codex - {label}**\n\n{self.content}"


def format_agent_card(text: str | dict, title: str = "**🔀 Agent**") -> str:
    """构建 Agent Telegram 文本（供 core_client.py 直接调用）。"""
    if isinstance(text, dict):
        data = text
    elif text and text.strip().startswith("{"):
        try:
            import json
            data = json.loads(text)
        except Exception:
            data = None
    else:
        data = None

    if data and isinstance(data, dict):
        parts = [title]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---\n")
        return "\n".join(parts)
    return f"{title}\n\n{text or ''}"


def format_codex_card(event_type: str, content: str = "", extra: dict | None = None) -> str:
    """构建 Codex 事件 Telegram 文本（供 core_client.py 直接调用）。"""
    marker = build_codex_marker(event_type, content, extra)
    return marker.render()
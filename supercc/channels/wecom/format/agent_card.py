"""Agent / Codex 渲染 — WeCom markdown 版本（复用 common.format 基类）。"""
from __future__ import annotations
import json

from supercc.channels.common.format.agent import _AgentCardMarker, _CodexMarker


def format_agent_markdown(tool_input: str, title: str = "🤖 Agent") -> str:
    """将 Agent 响应格式化为 WeCom markdown（复用 common 基类）。"""
    marker = _AgentCardMarker("Agent", tool_input)
    if marker.data and isinstance(marker.data, dict):
        parts = [f"**{title}**"]
        for key, value in marker.data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---\n")
        return "\n".join(parts)
    return f"**{title}**\n\n{tool_input or ''}"


def format_codex_markdown(event_type: str, content: str = "", extra: dict | None = None) -> str:
    """将 Codex 事件格式化为 WeCom markdown（复用 common 基类）。"""
    marker = _CodexMarker(event_type, content, extra)
    return marker.render()

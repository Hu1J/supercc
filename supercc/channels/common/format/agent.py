"""Agent / Codex 渲染 — 平台无关基类。"""
from __future__ import annotations
import json


class _AgentCardMarker:
    """Agent 响应卡片标记，平台无关实现 render() 为纯 markdown。

    各平台可继承并覆盖 render() 输出平台原生格式。
    """
    __slots__ = ("tool_name", "tool_input", "data")

    def __init__(self, tool_name: str, tool_input: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        parsed = self._parse(tool_input)
        self.data = parsed  # None 或 dict

    def _parse(self, tool_input: str) -> dict | None:
        try:
            return json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            return None

    def render(self) -> str:
        """渲染为纯 markdown（平台无关兜底实现）。"""
        data = self.data
        if data and isinstance(data, dict):
            parts = ["**🔀 Agent**"]
            for key, value in data.items():
                parts.append(f"**{key}**: {value}")
                parts.append("\n---\n")
            return "\n".join(parts)
        return f"**🔀 {self.tool_name}**\n\n{self.tool_input or ''}"


class _CodexMarker:
    """Codex 事件卡片标记，平台无关实现 render() 为纯 markdown。"""
    __slots__ = ("event_type", "content", "extra", "tool_input")

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

    def __init__(self, event_type: str, content: str = "", extra: dict | None = None, tool_input: str = ""):
        self.event_type = event_type
        self.content = content
        self.extra = extra or {}
        self.tool_input = tool_input

    def render(self) -> str:
        """渲染为纯 markdown（平台无关兜底实现）。"""
        icon = self.ICONS.get(self.event_type, "🔀")
        label = self._event_label()
        return f"**{icon} Codex - {label}**\n\n{self.content}"

    def _event_label(self) -> str:
        et = self.event_type
        extra = self.extra
        tool_name = extra.get("tool_name", "") if isinstance(extra, dict) else ""

        if et == "text":
            return "content"
        if et == "tool_use":
            return tool_name if tool_name else "tool"
        if et == "command_execution":
            return "command"
        if et == "command_output":
            return "output"
        if et == "file_change":
            return "file"
        if et == "reasoning":
            return "reasoning"
        if et == "todo_list":
            return "todo"
        if et == "error":
            return "error"
        if et == "finished":
            return "finished"
        if et == "started":
            return "started"
        return et or "event"


def build_codex_marker(event_type: str, content: str = "", extra: dict | None = None) -> _CodexMarker:
    """从 Codex WS 事件数据构建 marker。"""
    return _CodexMarker(event_type, content, extra)

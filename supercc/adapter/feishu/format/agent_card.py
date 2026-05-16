"""Agent / Codex 响应飞书卡片 — 继承 common.format 基类，覆盖 render() 为 CardKit。"""
from __future__ import annotations
import json

from supercc.adapter.common.format.agent import _AgentCardMarker, _CodexMarker
from supercc.adapter.feishu.format.markdown_util import optimize_markdown_style


class FeishuAgentCardMarker(_AgentCardMarker):
    """Agent 卡片 — 飞书平台覆盖 render() 输出 CardKit dict。"""

    def render(self) -> dict:
        """构建 Agent 飞书卡片。"""
        data = self.data
        if data and isinstance(data, dict):
            parts = ["## 🤖 Agent"]
            for key, value in data.items():
                parts.append(f"**{key}**: {value}")
                parts.append("\n---\n")
            content = "\n".join(parts)
        else:
            content = optimize_markdown_style(self.tool_input or "", card_version=2)
            content = f"## 🤖 Agent\n\n{content}"

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                ]
            },
        }


class FeishuCodexMarker(_CodexMarker):
    """Codex 卡片 — 飞书平台覆盖 render() 输出 CardKit dict。"""

    def render(self) -> dict:
        """构建 Codex 事件飞书卡片。"""
        title = f"## 🤖 Codex - {self._event_label()}"
        body = optimize_markdown_style(self.content or "", card_version=2)
        content = title if not body else f"{title}\n\n{body}"
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                ]
            },
        }


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


def format_agent_card(text: str | dict, title: str = "## 🤖 Agent") -> dict:
    """构建 Agent 飞书卡片（供 core_client.py 直接调用）。"""
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
            parts.append("\n---\n")
        content = "\n".join(parts)
    else:
        content = optimize_markdown_style(text or "", card_version=2)
        content = f"{title}\n\n{content}"

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
            ]
        },
    }


def format_codex_card(event_type: str, content: str = "", extra: dict | None = None) -> dict:
    """构建 Codex 事件飞书卡片（供 core_client.py 直接调用）。"""
    title = f"## 🤖 Codex - {_codex_event_label(event_type, extra)}"
    body = optimize_markdown_style(content or "", card_version=2)
    card_content = title if not body else f"{title}\n\n{body}"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": card_content},
            ]
        },
    }

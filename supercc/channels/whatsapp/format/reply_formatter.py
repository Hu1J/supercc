"""WhatsApp 消息格式化工具。"""
from __future__ import annotations

import json
import re

from supercc.channels.whatsapp.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker
from supercc.channels.common.format import MemoryCardMarker
from supercc.channels.whatsapp.format.questionnaire_card import _AskUserQuestionMarker
from supercc.channels.whatsapp.format.agent_card import WhatsAppAgentCardMarker, WhatsAppCodexMarker
from supercc.channels.whatsapp.format.markdown_util import optimize_markdown_style

WHATSAPP_MAX_MESSAGE_LENGTH = 4096


def should_use_card(text: str) -> bool:
    """Decide whether content should use card format.

    For WhatsApp, this always returns False since WhatsApp doesn't support
    interactive cards. Kept for API compatibility with core_protocol.py.
    """
    return False


class ReplyFormatter:
    """WhatsApp 消息格式化器。"""

    def __init__(self):
        self.tool_icons = {
            "Read": "📖",
            "Write": "✏️",
            "Edit": "🔧",
            "Bash": "💻",
            "Glob": "🔍",
            "Grep": "🔎",
            "WebFetch": "🌐",
            "WebSearch": "🌐",
            "Task": "📋",
            "MemorySearch": "🧠",
            "MemoryList": "🧠",
            "MemoryAdd": "🧠",
            "MemoryDelete": "🧠",
            "MemoryClear": "🧠",
            "WhatsAppSendFile": "📬",
            "Skill": "🧰",
            "SkillSearch": "🎯",
            "SkillInvoke": "🧰",
            "EnterPlanMode": "🎯",
            "ExitPlanMode": "🎯",
            "AskUserQuestion": "🎯",
            "Agent": "🔀",
            "mcp__codex__codex": "⚡",
        }

    def format_text(self, text: str) -> str:
        """Prepare Markdown text for WhatsApp rendering.

        Converts standard Markdown to WhatsApp-compatible format:
        - **bold** → *bold*
        - ~~strike~~ → ~strike~
        - # Header → *Header*
        - [text](url) → text: url
        - `code` preserved
        - ```code block``` preserved
        """
        if not text:
            return ""
        return optimize_markdown_style(text).strip()

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str | _DiffMarker | list[_DiffMarker] | MemoryCardMarker | _AskUserQuestionMarker | WhatsAppAgentCardMarker | WhatsAppCodexMarker:
        """Format a tool call notification for the user.

        Returns _DiffMarker for Edit/Write tools, or a plain string for all other tools.
        """
        if tool_input is None:
            tool_input = ""

        # Edit / Write → 文本 diff
        if tool_name == "Edit":
            if tool_input.strip():
                try:
                    return build_edit_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式
        elif tool_name == "Write":
            if tool_input.strip():
                try:
                    return build_write_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式

        # Bash → md 代码段
        elif tool_name == "Bash":
            return self._format_bash_tool(tool_input)

        # TodoWrite → markdown 表格
        elif tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input)

        # AskUserQuestion → 文本问卷
        elif tool_name == "AskUserQuestion":
            marker = _AskUserQuestionMarker(tool_name, tool_input)
            if marker.data is not None:
                return marker
            return f"🔀 **{tool_name}**\n`{tool_input}`"

        # Memory MCP tools → 文本格式
        elif tool_name and tool_name.startswith("mcp__SuperCC__Memory"):
            return self._format_memory_tool(tool_name, tool_input, **kwargs)

        # Cron MCP tools → ⏰ 时钟图标
        elif tool_name and tool_name.startswith("mcp__SuperCC__Cron"):
            short_name = tool_name.replace("mcp__SuperCC__", "")
            icon = "⏰"
            msg = f"{icon} **{short_name}**"
            if tool_input and len(tool_input) <= WHATSAPP_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            return msg

        # 其他 mcp__SuperCC__ 工具 → 去掉前缀后走通用逻辑
        elif tool_name and tool_name.startswith("mcp__SuperCC__"):
            tool_name = tool_name.replace("mcp__SuperCC__", "")

        # Read → 提取 file_path，用 backtick 包裹
        elif tool_name == "Read":
            return self._format_read_tool(tool_input)

        # Agent (sub-agent) → 文本卡片
        elif tool_name == "Agent":
            return self._format_agent_tool(tool_input)

        # mcp__codex__codex → 文本卡片
        elif tool_name == "mcp__codex__codex":
            return self._format_codex_tool(tool_input)

        # 其他工具 → backtick 格式
        icon = self.tool_icons.get(tool_name, "🤖")
        msg = f"{icon} **{tool_name}**"
        if tool_input:
            if len(tool_input) <= WHATSAPP_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            else:
                chunks = self.split_messages(tool_input)
                for chunk in chunks:
                    msg += f"\n`{chunk}`"
        return msg

    def _format_bash_tool(self, tool_input: str) -> str:
        """Format Bash tool call as a markdown code block."""
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            return f"💻 **Bash**\n```bash\n{tool_input}\n```"

        command = data.get("command", tool_input)
        description = data.get("description")

        icon = self.tool_icons.get("Bash", "💻")
        if description:
            header = f"{icon} **Bash** — {description}"
        else:
            header = f"{icon} **Bash**"

        return f"{header}\n```bash\n{command}\n```"

    def _format_read_tool(self, tool_input: str) -> str:
        """Format Read tool call with backtick-wrapped file path."""
        if not tool_input:
            return ""
        try:
            data = json.loads(tool_input)
            file_path = data.get("file_path", tool_input)
        except (json.JSONDecodeError, TypeError):
            file_path = tool_input
            data = {}

        icon = self.tool_icons.get("Read", "📖")
        extras = []
        if data.get("offset") is not None:
            extras.append(f"offset {data['offset']}")
        if data.get("limit") is not None:
            extras.append(f"limit {data['limit']}")

        if extras:
            title = f"**Read** — " + " — ".join(extras)
            path_line = f"`{file_path}`"
        else:
            title = f"**Read**"
            path_line = f"`{file_path}`"
        return f"{icon} {title}\n{path_line}"

    def _format_agent_tool(self, tool_input: str) -> WhatsAppAgentCardMarker:
        """Format Agent (sub-agent) tool call → WhatsAppAgentCardMarker。"""
        return WhatsAppAgentCardMarker("Agent", tool_input)

    def _format_codex_tool(self, tool_input: str) -> WhatsAppCodexMarker:
        """Format mcp__codex__codex tool call → WhatsAppCodexMarker。"""
        try:
            data = json.loads(tool_input) if tool_input else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        prompt = data.get("prompt", tool_input or "")
        model = data.get("model", "")
        event_type = "text"
        extra = {"model": model} if model else None
        return WhatsAppCodexMarker(event_type, prompt, extra, tool_input)

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        **kwargs,
    ) -> str:
        """格式化记忆 MCP 工具调用为文本。"""
        short_name = tool_name.replace("mcp__SuperCC__", "")
        icon = "🧠"
        msg = f"{icon} **{short_name}**"
        if tool_input and len(tool_input) <= WHATSAPP_MAX_MESSAGE_LENGTH - len(msg) - 5:
            msg += f"\n`{tool_input}`"
        return msg

    def _format_todowrite_tool(self, tool_input: str) -> str:
        """Format TodoWrite tool call as a markdown table."""
        try:
            data = json.loads(tool_input)
            todos = data.get("todos", [])
        except json.JSONDecodeError:
            todos = []

        if not isinstance(todos, list):
            todos = []

        if not todos:
            return "✅ 所有任务已完成！"

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        rows = ["| 状态 | 待办事项 | 当前动作 |", "|------|----------|----------|"]
        for t in todos:
            icon = status_icon.get(t.get("status", "pending"), "⬜")
            content = str(t.get("content", "")).replace("\n", " ").replace("|", "\\|")
            active = str(t.get("activeForm", "")).replace("\n", " ").replace("|", "\\|")
            rows.append(f"| {icon} | {content} | {active} |")

        return "📋 Todo List\n\n" + "\n".join(rows)

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under WhatsApp's limit."""
        if len(text) <= WHATSAPP_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= WHATSAPP_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                if len(line) > WHATSAPP_MAX_MESSAGE_LENGTH:
                    while len(line) > WHATSAPP_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:WHATSAPP_MAX_MESSAGE_LENGTH])
                        line = line[WHATSAPP_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]


def format_whatsapp_markdown(text: str) -> str:
    """
    将标准 Markdown 转换为 WhatsApp 兼容格式。

    WhatsApp 不支持标准 Markdown，转换规则：
    - **bold** → *bold*
    - ~~strike~~ → ~strike~
    - # Header → *Header*（简单标题）
    - [text](url) → text: url
    - `code` 保持不变（WhatsApp 支持）
    - ```code block``` 保持不变

    注意：WhatsApp 对格式的支持有限，长消息会被分割。
    """
    if not text:
        return ""

    # 预处理：保护代码块
    code_blocks: list[str] = []

    def protect_code(match):
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    text = re.sub(r"```[\s\S]*?```", protect_code, text)
    text = re.sub(r"`[^`]+`", protect_code, text)

    # 粗体：**text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 删除线：~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 标题：# H → *H*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 链接：[text](url) → text: url
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1: \2", text)

    # 恢复代码块
    for i, block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", block)

    return text


def format_tool_result(tool_name: str, result: str) -> str:
    """
    格式化工具调用结果为 WhatsApp 消息。

    Args:
        tool_name: 工具名称
        result: 工具返回结果

    Returns:
        格式化后的文本
    """
    # 通用格式化：工具名 + 结果摘要
    short_name = tool_name.replace("mcp__SuperCC__", "")
    truncated = result[:200] + "..." if len(result) > 200 else result
    return f"[{short_name}]\n{truncated}"
"""彩色 diff 渲染 — Edit/Write 工具专用（WhatsApp 平台）。

WhatsApp 不支持卡片格式，使用纯文本 diff：
- 删除行：🔴 - content
- 添加行：🟢 + content
- 上下文：  content
"""
from __future__ import annotations
import json

from supercc.channels.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine


# WhatsApp emoji indicators
EMOJI_DELETION = "🔴"
EMOJI_INSERTION = "🟢"


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（WhatsApp 平台扩展）。"""

    def prefix(self) -> str:
        if self.type == "deletion":
            return f"{EMOJI_DELETION} - "
        elif self.type == "insertion":
            return f"{EMOJI_INSERTION} + "
        return "    "


MAX_DIFF_LINES = 30      # 单次消息最大行数


def _format_diff_text(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为纯文本，每行带行号和 emoji 标记。"""
    if not diff_lines:
        return ""
    # 计算行号位数，零填充对齐
    digits = len(str(len(diff_lines)))

    parts = []
    for i, d in enumerate(diff_lines, 1):
        line = f"{d.prefix()}{d.content}"
        line_no_str = str(i).zfill(digits)
        parts.append(f"{line_no_str} │ {line}")
    return "\n".join(parts)


def format_edit_diff(file_path: str, diff_lines: list[_BaseDiffLine]) -> str:
    """构建 Edit 工具的 WhatsApp 纯文本 diff。"""
    header = f"✏️ **Edit** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    return f"{header}\n\n{diff_text}"


def format_write_diff(file_path: str, content_lines: list[str]) -> str:
    """构建 Write 工具的 WhatsApp 纯文本 diff（全量显示）。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header = f"📝 **Write** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    return f"{header}\n\n{diff_text}"


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 WhatsAppCoreWSClient 此工具调用需要渲染文本 diff。"""
    __slots__ = ("tool_name", "tool_input", "diff_text")

    def __init__(self, tool_name: str, tool_input: str, diff_text: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.diff_text = diff_text    # 预格式化的纯文本 diff

    def render(self) -> str:
        """渲染为纯文本 diff。"""
        data = json.loads(self.tool_input)
        file_path = data.get("file_path", "unknown")
        if self.tool_name == "Edit":
            old_str = data.get("old_string", "")
            new_str = data.get("new_string", "")
        elif self.tool_name == "Write":
            old_str = ""
            new_str = data.get("content", "")
        else:
            return f"**{self.tool_name}** — `{file_path}`"

        diff = colorize_diff(old_str, new_str)
        diff_text = "\n".join(f"{d.prefix()}{d.content}" for d in diff)
        lines = [f"**{file_path}**\n"]
        lines.append(f"```diff\n{diff_text}\n```")
        return "\n".join(lines)


def build_edit_marker(tool_input_json: str) -> _DiffMarker:
    """从 Edit 工具的 tool_input JSON 构建 marker。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    old_str = data.get("old_string", "")
    new_str = data.get("new_string", "")
    diff = colorize_diff(old_str, new_str)
    diff_text = format_edit_diff(file_path, diff)
    return _DiffMarker("Edit", tool_input_json, diff_text)


def build_write_marker(tool_input_json: str) -> list[_DiffMarker]:
    """从 Write 工具的 tool_input JSON 构建 marker list（过长时分块）。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    content = data.get("content", "")
    lines = content.splitlines()
    # Write 过长时分块：每块 MAX_DIFF_LINES 行
    if len(lines) <= MAX_DIFF_LINES:
        diff_text = format_write_diff(file_path, lines)
        return [_DiffMarker("Write", tool_input_json, diff_text)]
    chunks = [lines[i:i + MAX_DIFF_LINES] for i in range(0, len(lines), MAX_DIFF_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_diff(file_path, chunk)) for chunk in chunks]
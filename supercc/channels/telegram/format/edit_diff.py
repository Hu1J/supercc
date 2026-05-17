"""彩色 diff 渲染 — Edit/Write 工具专用（Telegram 平台）。

Telegram 不支持 CardKit，使用 emoji + markdown 格式展示 diff。
"""
from __future__ import annotations
import json

from supercc.channels.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine

MAX_LINES = 30      # 最大行数


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（Telegram 平台扩展）。"""

    def prefix(self) -> str:
        if self.type == "deletion":
            return "🔴 - "
        elif self.type == "insertion":
            return "🟢 + "
        return "   "

    def color(self) -> str:
        if self.type == "deletion":
            return "red"
        elif self.type == "insertion":
            return "green"
        return "grey"


def _format_diff_text(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为纯文本，每行带行号（行号右对齐）和 emoji 前缀。"""
    if not diff_lines:
        return ""
    # 计算行号位数，零填充对齐
    digits = len(str(len(diff_lines)))

    parts = []
    for i, d in enumerate(diff_lines, 1):
        line = d.content
        line_no_str = str(i).zfill(digits)
        if d.type == "deletion":
            colored = f"🔴 {line_no_str} │ {d.prefix()}{line}"
        elif d.type == "insertion":
            colored = f"🟢 {line_no_str} │ {d.prefix()}{line}"
        else:
            colored = f"   {line_no_str} │ {d.prefix()}{line}"
        parts.append(colored)
    return "\n".join(parts)


def format_edit_text(file_path: str, diff_lines: list[_BaseDiffLine]) -> str:
    """构建 Edit 工具的 Telegram 文本格式。"""
    header = f"✏️ **Edit** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    if diff_text:
        return f"{header}\n\n{diff_text}"
    return header


def format_write_text(file_path: str, content_lines: list[str]) -> str:
    """构建 Write 工具的 Telegram 文本格式。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header = f"📝 **Write** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    if diff_text:
        return f"{header}\n\n{diff_text}"
    return header


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 TelegramCoreWSClient 此工具调用需要渲染 emoji diff 文本。"""
    __slots__ = ("tool_name", "tool_input", "text")

    def __init__(self, tool_name: str, tool_input: str, text: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.text = text              # 预渲染的文本

    def render(self) -> str:
        """渲染为纯文本 diff（用于日志等）。"""
        return self.text


def build_edit_marker(tool_input_json: str) -> _DiffMarker:
    """从 Edit 工具的 tool_input JSON 构建 marker。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    old_str = data.get("old_string", "")
    new_str = data.get("new_string", "")
    diff = colorize_diff(old_str, new_str)
    text = format_edit_text(file_path, diff)
    return _DiffMarker("Edit", tool_input_json, text)


def build_write_marker(tool_input_json: str) -> list[_DiffMarker]:
    """从 Write 工具的 tool_input JSON 构建 marker list（过长时分块）。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    content = data.get("content", "")
    lines = content.splitlines()
    # Write 过长时分块：每块 MAX_LINES 行
    if len(lines) <= MAX_LINES:
        text = format_write_text(file_path, lines)
        return [_DiffMarker("Write", tool_input_json, text)]
    chunks = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_text(file_path, chunk)) for chunk in chunks]
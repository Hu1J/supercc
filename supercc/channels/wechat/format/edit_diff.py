"""彩色 diff 渲染 — Edit/Write 工具专用（WeChat 平台）。
WeChat 不支持 CardKit，使用 emoji + markdown 格式。
"""
from __future__ import annotations
import json

from supercc.channels.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine

MAX_DIFF_LINES = 30      # 单次最大行数


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（WeChat 平台扩展）。"""

    def prefix(self) -> str:
        if self.type == "deletion":
            return "🔴 - "
        elif self.type == "insertion":
            return "🟢 + "
        return "⚪   "


def _format_diff_text(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为纯文本，每行带 emoji 标记。"""
    if not diff_lines:
        return ""
    # 计算行号位数，零填充对齐
    digits = len(str(len(diff_lines)))

    parts = []
    for i, d in enumerate(diff_lines, 1):
        d_line = d  # type: _BaseDiffLine
        line = f"{d_line.prefix()}{d_line.content}"
        line_no_str = str(i).zfill(digits)
        parts.append(f"{line_no_str} │ {line}")
    return "\n".join(parts)


def format_edit_text(file_path: str, diff_lines: list[_BaseDiffLine]) -> str:
    """构建 Edit 工具的 markdown diff 文本。"""
    header = f"**✏️ Edit** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    return f"{header}\n\n{diff_text}"


def format_write_text(file_path: str, content_lines: list[str]) -> str:
    """构建 Write 工具的 markdown 全量文本。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header = f"**📝 Write** — `{file_path}`"
    diff_text = _format_diff_text(diff_lines)
    return f"{header}\n\n{diff_text}"


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 WeChatCoreWSClient 此工具调用需要渲染彩色 diff 文本。"""
    __slots__ = ("tool_name", "tool_input", "text")

    def __init__(self, tool_name: str, tool_input: str, text: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.text = text              # 预构建的 markdown 文本

    def render(self) -> str:
        """渲染为纯文本 diff。"""
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
    # Write 过长时分块：每块 MAX_DIFF_LINES 行
    if len(lines) <= MAX_DIFF_LINES:
        return [_DiffMarker("Write", tool_input_json, format_write_text(file_path, lines))]
    chunks = [lines[i:i + MAX_DIFF_LINES] for i in range(0, len(lines), MAX_DIFF_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_text(file_path, chunk)) for chunk in chunks]
"""彩色 diff 渲染 — Edit/Write 工具专用（钉钉平台）。

钉钉使用 markdown 格式，diff 以 ```diff 代码块 渲染。
"""
from __future__ import annotations
import json

from supercc.channels.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine

MAX_CARD_LINES = 30      # 单次卡片最大行数


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（钉钉平台扩展）。"""

    def prefix(self) -> str:
        if self.type == "deletion":
            return "- "
        elif self.type == "insertion":
            return "+ "
        return "  "


def _format_diff_markdown(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为 markdown diff 文本，每行带行号（行号右对齐）。"""
    if not diff_lines:
        return ""
    # 计算行号位数，零填充对齐
    digits = len(str(len(diff_lines)))

    parts = []
    for i, d in enumerate(diff_lines, 1):
        line = f"{d.prefix()}{d.content}"
        line_no_str = str(i).zfill(digits)
        if d.type == "deletion":
            colored = f"~~{line_no_str} │ {line}~~"  # 删除线表示删除
        elif d.type == "insertion":
            colored = f"**{line_no_str} │ {line}**"  # 粗体表示新增
        else:
            colored = f"{line_no_str} │ {line}"
        parts.append(colored)
    return "\n".join(parts)


def format_edit_markdown(file_path: str, diff_lines: list[_BaseDiffLine]) -> str:
    """构建 Edit 工具的钉钉 markdown 消息。"""
    header_md = f"✏️ **Edit** — `{file_path}`"
    diff_md = _format_diff_markdown(diff_lines)
    return f"{header_md}\n\n{diff_md}"


def format_write_markdown(file_path: str, content_lines: list[str]) -> str:
    """构建 Write 工具的钉钉 markdown 全量消息。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header_md = f"📝 **Write** — `{file_path}`"
    diff_md = _format_diff_markdown(diff_lines)
    return f"{header_md}\n\n{diff_md}"


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 DingTalkCoreWSClient 此工具调用需要渲染 markdown diff。"""
    __slots__ = ("tool_name", "tool_input", "content")

    def __init__(self, tool_name: str, tool_input: str, content: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.content = content       # 预构建的 markdown 内容

    def render(self) -> str:
        """渲染为纯文本 diff（cron verbose 模式使用）。"""
        return self.content


def build_edit_marker(tool_input_json: str) -> _DiffMarker:
    """从 Edit 工具的 tool_input JSON 构建 marker。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    old_str = data.get("old_string", "")
    new_str = data.get("new_string", "")
    diff = colorize_diff(old_str, new_str)
    content = format_edit_markdown(file_path, diff)
    return _DiffMarker("Edit", tool_input_json, content)


def build_write_marker(tool_input_json: str) -> list[_DiffMarker]:
    """从 Write 工具的 tool_input JSON 构建 marker list（过长时分块）。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    content = data.get("content", "")
    lines = content.splitlines()
    # Write 过长时分块：每块 MAX_CARD_LINES 行
    if len(lines) <= MAX_CARD_LINES:
        return [_DiffMarker("Write", tool_input_json, format_write_markdown(file_path, lines))]
    chunks = [lines[i:i + MAX_CARD_LINES] for i in range(0, len(lines), MAX_CARD_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_markdown(file_path, chunk)) for chunk in chunks]
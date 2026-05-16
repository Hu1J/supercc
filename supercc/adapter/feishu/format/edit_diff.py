"""彩色 diff 渲染 — Edit/Write 工具专用（飞书平台）。"""
from __future__ import annotations
import json

from supercc.adapter.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine

# 飞书 plain_text 支持的颜色
COLOR_RED = "red"
COLOR_GREEN = "green"    # 注：浅色主题下偏淡，可调整
COLOR_GREY = "grey"
COLOR_BLUE = "blue"
COLOR_DEFAULT = "default"

MAX_CARD_LINES = 30      # 单次卡片最大行数


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（飞书平台扩展）。"""

    def color(self) -> str:
        if self.type == "deletion":
            return COLOR_RED
        elif self.type == "insertion":
            return COLOR_GREEN
        return COLOR_GREY

    def prefix(self) -> str:
        if self.type == "deletion":
            return "- "
        elif self.type == "insertion":
            return "+ "
        return "  "


def _format_diff_lark_md(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为 lark_md 文本，每行带行号（行号右对齐）和颜色标签。"""
    if not diff_lines:
        return ""
    # 计算行号位数，零填充对齐（避免等宽字体下空格对齐不可靠）
    digits = len(str(len(diff_lines)))

    parts = []
    for i, d in enumerate(diff_lines, 1):
        line = f"{d.prefix()}{d.content}"
        line_no_str = str(i).zfill(digits)
        if d.type == "deletion":
            colored = f"<font color='red'>{line_no_str} │ {line}</font>"
        elif d.type == "insertion":
            colored = f"<font color='green'>{line_no_str} │ {line}</font>"
        else:
            colored = f"{line_no_str} │ {line}"
        parts.append(colored)
    return "\n".join(parts)


def format_edit_card(file_path: str, diff_lines: list[_BaseDiffLine]) -> dict:
    """构建 Edit 工具的飞书 diff 卡片。"""
    header_md = f"✏️ **Edit** — `{file_path}`"
    diff_md = _format_diff_lark_md(diff_lines)
    elements = [
        {
            "tag": "markdown",
            "content": header_md,
        },
        {
            "tag": "markdown",
            "content": diff_md,
        },
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def format_write_card(file_path: str, content_lines: list[str]) -> dict:
    """构建 Write 工具的飞书全量卡片。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header_md = f"📝 **Write** — `{file_path}`"
    diff_md = _format_diff_lark_md(diff_lines)
    elements = [
        {
            "tag": "markdown",
            "content": header_md,
        },
        {
            "tag": "markdown",
            "content": diff_md,
        },
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 FeishuCoreWSClient 此工具调用需要渲染彩色 diff 卡片。"""
    __slots__ = ("tool_name", "tool_input", "card")

    def __init__(self, tool_name: str, tool_input: str, card: dict):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.card = card              # 预构建的飞书卡片 JSON

    def render(self) -> str:
        """渲染为纯文本 diff（cron verbose 模式使用）。"""
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
    card = format_edit_card(file_path, diff)
    return _DiffMarker("Edit", tool_input_json, card)


def build_write_marker(tool_input_json: str) -> list[_DiffMarker]:
    """从 Write 工具的 tool_input JSON 构建 marker list（过长时分块）。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    content = data.get("content", "")
    lines = content.splitlines()
    # Write 过长时分块：每块 MAX_CARD_LINES 行
    if len(lines) <= MAX_CARD_LINES:
        return [_DiffMarker("Write", tool_input_json, format_write_card(file_path, lines))]
    chunks = [lines[i:i + MAX_CARD_LINES] for i in range(0, len(lines), MAX_CARD_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_card(file_path, chunk)) for chunk in chunks]

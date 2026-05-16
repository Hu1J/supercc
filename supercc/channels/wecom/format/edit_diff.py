"""彩色 diff 渲染 — Edit/Write 工具专用（WeCom markdown 版本）。"""
from __future__ import annotations

from supercc.channels.common.format.diff import colorize_diff, DiffLine as _BaseDiffLine


class DiffLine(_BaseDiffLine):
    """一行 diff 结果（WeCom 平台扩展）。"""

    def prefix(self) -> str:
        if self.type == "deletion":
            return "- "
        elif self.type == "insertion":
            return "+ "
        return "  "

    def emoji(self) -> str:
        if self.type == "deletion":
            return "🔴"
        elif self.type == "insertion":
            return "🟢"
        return "  "


def format_diff_markdown(diff_lines: list[_BaseDiffLine]) -> str:
    """将 diff_lines 格式化为 WeCom markdown 代码块（使用 emoji + 符号）。"""
    if not diff_lines:
        return ""

    digits = len(str(len(diff_lines)))
    lines = []
    for i, d in enumerate(diff_lines, 1):
        line_no = str(i).zfill(digits)
        prefix = d.prefix()
        emoji = d.emoji()
        content = d.content
        if d.type == "deletion":
            lines.append(f"🔴 `{line_no}` │ {prefix}{content}")
        elif d.type == "insertion":
            lines.append(f"🟢 `{line_no}` │ {prefix}{content}")
        else:
            lines.append(f"  `{line_no}` │ {prefix}{content}")

    return "```\n" + "\n".join(lines) + "\n```"


def format_edit_markdown(file_path: str, diff_lines: list[_BaseDiffLine]) -> str:
    """构建 Edit 工具的 WeCom markdown diff。"""
    header = f"✏️ **Edit** — `{file_path}`"
    diff = format_diff_markdown(diff_lines)
    return f"{header}\n{diff}"


def format_write_markdown(file_path: str, content_lines: list[str]) -> str:
    """构建 Write 工具的 WeCom markdown 全量。"""
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    header = f"📝 **Write** — `{file_path}`"
    diff = format_diff_markdown(diff_lines)
    return f"{header}\n{diff}"

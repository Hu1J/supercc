"""彩色 diff 渲染 — Edit/Write 工具专用（WeCom markdown 版本）。"""
from __future__ import annotations

MAX_DIFF_LINES = 50
CONTEXT_LINES = 3


class DiffLine:
    """一行 diff 结果。"""
    __slots__ = ("type", "content")

    def __init__(self, type: str, content: str):
        self.type = type  # "deletion" | "insertion" | "context"
        self.content = content

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


def colorize_diff(old_string: str, new_string: str) -> list[DiffLine]:
    """对 old_string 和 new_string 做行级 LCS，返回带类型的行列表。"""
    if not old_string and not new_string:
        return []
    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff = _lcs_diff(old_lines, new_lines)

    if len(diff) > MAX_DIFF_LINES:
        diff = _truncate_diff(diff)

    return diff


def _lcs_diff(old_lines: list[str], new_lines: list[str]) -> list[DiffLine]:
    m, n = len(old_lines), len(new_lines)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old_lines[i - 1] == new_lines[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    result = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and old_lines[i - 1] == new_lines[j - 1]:
            result.append(DiffLine("context", old_lines[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            result.append(DiffLine("insertion", new_lines[j - 1]))
            j -= 1
        else:
            result.append(DiffLine("deletion", old_lines[i - 1]))
            i -= 1

    result.reverse()
    return result


def _truncate_diff(diff: list[DiffLine]) -> list[DiffLine]:
    if len(diff) <= MAX_DIFF_LINES:
        return diff
    keep_head = diff[:CONTEXT_LINES]
    keep_tail = diff[-CONTEXT_LINES:] if len(diff) >= CONTEXT_LINES else diff
    return keep_head + [DiffLine("context", "...")] + keep_tail


def format_diff_markdown(diff_lines: list[DiffLine]) -> str:
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

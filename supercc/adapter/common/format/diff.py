"""Diff 渲染公共模块 — LCS 行级 diff 算法，各平台通用。"""
from __future__ import annotations

MAX_DIFF_LINES = 50
CONTEXT_LINES = 3


class DiffLine:
    """一行 diff 结果（平台无关）。"""
    __slots__ = ("type", "content")

    def __init__(self, type: str, content: str):
        self.type = type    # "deletion" | "insertion" | "context"
        self.content = content

    def prefix(self) -> str:
        """子类可覆盖。默认实现。"""
        if self.type == "deletion":
            return "- "
        elif self.type == "insertion":
            return "+ "
        return "  "

    def color(self) -> str:
        """子类可覆盖。默认实现。"""
        return "default"


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

    # Backtrack to get the diff
    i, j = m, n
    result = []
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
    """截断过长的 diff，保留首尾上下文。"""
    if len(diff) <= MAX_DIFF_LINES:
        return diff

    kept = diff[:CONTEXT_LINES]
    kept.append(DiffLine("context", "..."))
    kept.extend(diff[-(CONTEXT_LINES):])
    return kept

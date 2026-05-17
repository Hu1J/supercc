"""WeChat markdown 优化工具（供 reply_formatter 和 agent_card 共享）。
WeChat 是纯文本平台，markdown 支持有限（bold/code）。
"""
from __future__ import annotations
import re


_CODE_BLOCK_MARK = "___CB_"
_CODE_BLOCK_MARK_END = "___"


def optimize_markdown_style(text: str, card_version: int = 2) -> str:
    """Optimize Markdown for WeChat rendering.

    WeChat supports limited Markdown (bold, code), so we do light normalization.
    This is mostly a pass-through since WeChat doesn't have CardKit.
    """
    try:
        text = _optimize_markdown_style_impl(text, card_version)
        return text
    except Exception:
        return text


def _optimize_markdown_style_impl(text: str, card_version: int = 2) -> str:
    # 1. Protect code blocks with placeholders
    code_blocks: list[str] = []
    def _code_block_replacer(m):
        code_blocks.append(m.group(0))
        return f"{_CODE_BLOCK_MARK}{len(code_blocks) - 1}{_CODE_BLOCK_MARK_END}"
    r = re.sub(r"```[\s\S]*?```", _code_block_replacer, text)

    # 2. Heading level reduction (only if H1-H3 exist in original)
    if re.search(r"^#{1,3} ", text, re.MULTILINE):
        r = re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=re.MULTILINE)  # H2-H6 → H5
        r = re.sub(r"^# (.+)$", r"#### \1", r, flags=re.MULTILINE)         # H1 → H4

    if card_version >= 2:
        # 3. Spacing between consecutive headings
        r = re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", r, flags=re.MULTILINE)

        # 4. Table spacing
        # 4a: non-table line directly before table row → add blank line first
        r = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", r, flags=re.MULTILINE)
        # 4b: add <br> before table
        r = re.sub(r"\n\n((?:\|.+\|[^\S\n]*\n?)+)", r"\n\n<br>\n\n\1", r)

        # 5. Restore code blocks
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{_CODE_BLOCK_MARK}{i}{_CODE_BLOCK_MARK_END}", block)
    else:
        # 5. Restore code blocks (no <br>)
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{_CODE_BLOCK_MARK}{i}{_CODE_BLOCK_MARK_END}", block)

    # 6. Collapse 3+ consecutive newlines to 2
    r = re.sub(r"\n{3,}", r"\n\n", r)
    return r


def _count_tables_outside_code_blocks(text: str) -> int:
    """Count markdown table rows that are not inside fenced code blocks."""
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    lines = stripped.split("\n")
    count = 0
    for line in lines:
        line = line.strip()
        if line.startswith("|") and "|" in line[1:]:
            count += 1
    return max(0, count - 1)  # subtract header row
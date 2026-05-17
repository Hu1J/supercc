"""DingTalk markdown 优化工具（供 reply_formatter 和 agent_card 共享）。"""
from __future__ import annotations
import re


_CODE_BLOCK_MARK = "___CB_"
_CODE_BLOCK_MARK_END = "___"


def optimize_markdown_style(text: str, card_version: int = 2) -> str:
    """Optimize Markdown for DingTalk rendering.

    DingTalk's markdown renderer is more limited than Feishu:
    - Heading levels are limited (H1-H6 supported but may render differently)
    - Table support is basic
    - Code blocks work well

    Args:
        text: The markdown text to optimize
        card_version: Reserved for future card format versions
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

    # 2. Heading level adjustment - DingTalk handles H1-H3 well, reduce deeper levels
    if re.search(r"^#{1,3} ", text, re.MULTILINE):
        r = re.sub(r"^#{4,6} (.+)$", r"### \1", r, flags=re.MULTILINE)  # H4-H6 → H3
        r = re.sub(r"^## (.+)$", r"### \1", r, flags=re.MULTILINE)       # H2 → H3

    # 3. Ensure blank line before numbered lists (DingTalk quirk)
    r = re.sub(r"([^\n])\n(\d+\. )", r"\1\n\n\2", r)

    # 4. Table spacing - add blank line before tables
    r = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", r, flags=re.MULTILINE)

    # 5. Restore code blocks
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
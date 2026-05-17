"""WhatsApp markdown 优化工具。

WhatsApp 对 markdown 支持有限，此模块提供基础优化函数。
由于 WhatsApp 不支持卡片格式，主要用于检测内容复杂度。
"""
from __future__ import annotations
import re


def optimize_markdown_style(text: str, card_version: int = 2) -> str:
    """WhatsApp  markdown 优化（由于 WhatsApp 格式有限，仅做基础清理）。

    - 折叠多余空行
    - 保护代码块
    """
    if not text:
        return ""

    # 保护代码块
    code_blocks: list[str] = []
    def _code_block_replacer(m):
        code_blocks.append(m.group(0))
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"
    r = re.sub(r"```[\s\S]*?```", _code_block_replacer, text)

    # 折叠多余空行
    r = re.sub(r"\n{3,}", r"\n\n", r)

    # 恢复代码块
    for i, block in enumerate(code_blocks):
        r = r.replace(f"__CODE_BLOCK_{i}__", block)

    return r


def _count_tables_outside_code_blocks(text: str) -> int:
    """Count markdown table rows that are not inside fenced code blocks.

    For WhatsApp, this is informational only since WhatsApp doesn't
    support table rendering in any special format.
    """
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    lines = stripped.split("\n")
    count = 0
    for line in lines:
        line = line.strip()
        if line.startswith("|") and "|" in line[1:]:
            count += 1
    return max(0, count - 1)  # subtract header row
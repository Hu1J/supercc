"""WhatsApp 消息格式化工具。"""
from __future__ import annotations

import re


def format_whatsapp_markdown(text: str) -> str:
    """
    将标准 Markdown 转换为 WhatsApp 兼容格式。

    WhatsApp 不支持标准 Markdown，转换规则：
    - **bold** → *bold*
    - ~~strike~~ → ~strike~
    - # Header → *Header*（简单标题）
    - [text](url) → text: url
    - `code` 保持不变（WhatsApp 支持）
    - ```code block``` 保持不变

    注意：WhatsApp 对格式的支持有限，长消息会被分割。
    """
    # 预处理：保护代码块
    code_blocks: list[str] = []

    def protect_code(match):
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    text = re.sub(r"```[\s\S]*?```", protect_code, text)
    text = re.sub(r"`[^`]+`", protect_code, text)

    # 粗体：**text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 删除线：~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 标题：# H → *H*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 链接：[text](url) → text: url
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1: \2", text)

    # 恢复代码块
    for i, block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", block)

    return text


def format_tool_result(tool_name: str, result: str) -> str:
    """
    格式化工具调用结果为 WhatsApp 消息。

    Args:
        tool_name: 工具名称
        result: 工具返回结果

    Returns:
        格式化后的文本
    """
    # 通用格式化：工具名 + 结果摘要
    short_name = tool_name.replace("mcp__SuperCC__", "")
    truncated = result[:200] + "..." if len(result) > 200 else result
    return f"[{short_name}]\n{truncated}"

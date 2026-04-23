"""Agent 响应飞书卡片 — 将 Claude 的最终响应渲染为精美的 Interactive Card。"""
from __future__ import annotations

from supercc.adapter.feishu.format.reply_formatter import optimize_markdown_style

# 超过此长度的文本使用卡片发送
_AGENT_CARD_MIN_LENGTH = 500


def format_agent_card(text: str) -> dict:
    """构建 Agent 响应飞书卡片。

    使用 Feishu CardKit markdown 元素，支持标题、代码块、表格等格式。
    """
    optimized = optimize_markdown_style(text, card_version=2)

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": optimized,
                },
            ]
        },
    }


def should_use_agent_card(text: str) -> bool:
    """判断是否使用卡片发送 Agent 响应。

    卡片用于：文本较长（>= _AGENT_CARD_MIN_LENGTH），
    或包含代码块/表格的内容（更好的渲染效果）。
    """
    if len(text) >= _AGENT_CARD_MIN_LENGTH:
        return True
    # 有代码块或表格也用卡片
    if "```" in text or ("\n|" in text and text.count("|") >= 4):
        return True
    return False

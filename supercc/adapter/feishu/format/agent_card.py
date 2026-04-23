"""Agent 响应飞书卡片 — 将 Claude 的最终响应渲染为精美的 Interactive Card。"""
from __future__ import annotations

from supercc.adapter.feishu.format.reply_formatter import optimize_markdown_style

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


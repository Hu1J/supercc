"""Agent 响应飞书卡片 — 将 Claude 的最终响应渲染为精美的 Interactive Card。"""
from __future__ import annotations

import json

from supercc.adapter.feishu.format.reply_formatter import optimize_markdown_style


def format_agent_card(text: str) -> dict:
    """构建 Agent 响应飞书卡片。

    tool_input 为 JSON 字符串时，解析为 key-value 对用 markdown 展示，
    字段间用 `--` 分割。非 JSON 时直接渲染为 markdown。
    """
    # 尝试解析 JSON
    data = None
    if text and text.strip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass

    if data and isinstance(data, dict):
        parts = ["## 🤖 Agent"]
        for key, value in data.items():
            parts.append(f"**{key}**: {value}")
            parts.append("\n---\n")
        content = "\n".join(parts)
    else:
        # 非 JSON：直接走 markdown 优化
        content = optimize_markdown_style(text or "", card_version=2)
        content = f"## 🤖 Agent\n\n{content}"

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": content,
                },
            ]
        },
    }


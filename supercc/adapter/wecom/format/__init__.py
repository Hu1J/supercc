"""企业微信消息格式化 — 复用飞书 ReplyFormatter 的核心逻辑。"""
from supercc.adapter.feishu.format.reply_formatter import (
    ReplyFormatter,
    should_use_card,
    split_messages,
)

__all__ = ["ReplyFormatter", "should_use_card", "split_messages"]

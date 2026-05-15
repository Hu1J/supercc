"""企业微信消息格式化 — 复用飞书 ReplyFormatter 的核心逻辑。"""
from supercc.adapter.feishu.format.reply_formatter import (
    ReplyFormatter,
    should_use_card,
    split_messages,
)
from supercc.adapter.wecom.format.edit_diff import (
    colorize_diff,
    format_diff_markdown,
)
from supercc.adapter.wecom.format.questionnaire_card import (
    format_questionnaire_markdown,
)
from supercc.adapter.wecom.format.agent_card import (
    format_agent_markdown,
    format_codex_markdown,
)

__all__ = [
    "ReplyFormatter",
    "should_use_card",
    "split_messages",
    "colorize_diff",
    "format_diff_markdown",
    "format_questionnaire_markdown",
    "format_agent_markdown",
    "format_codex_markdown",
]

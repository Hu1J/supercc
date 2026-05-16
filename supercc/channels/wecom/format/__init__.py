"""企业微信消息格式化。"""
from supercc.channels.wecom.format.edit_diff import (
    colorize_diff,
    format_diff_markdown,
    format_edit_markdown,
    format_write_markdown,
)
from supercc.channels.wecom.format.questionnaire_card import (
    format_questionnaire_markdown,
)
from supercc.channels.wecom.format.agent_card import (
    format_agent_markdown,
    format_codex_markdown,
)

__all__ = [
    "colorize_diff",
    "format_diff_markdown",
    "format_edit_markdown",
    "format_write_markdown",
    "format_questionnaire_markdown",
    "format_agent_markdown",
    "format_codex_markdown",
]

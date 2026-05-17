"""Telegram format module."""
from supercc.channels.telegram.format.reply_formatter import ReplyFormatter, should_use_card
from supercc.channels.telegram.format.markdown_util import optimize_markdown_style
from supercc.channels.telegram.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker
from supercc.channels.telegram.format.agent_card import TelegramAgentCardMarker, TelegramCodexMarker
from supercc.channels.telegram.format.questionnaire_card import _AskUserQuestionMarker
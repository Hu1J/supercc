"""公共渲染模块 — 各平台可复用的样式逻辑（兜底实现）。"""
from supercc.adapter.common.format.diff import (
    DiffLine,
    colorize_diff,
    _lcs_diff,
    _truncate_diff,
    MAX_DIFF_LINES,
    CONTEXT_LINES,
)
from supercc.adapter.common.format.memory import MemoryCardMarker
from supercc.adapter.common.format.agent import _AgentCardMarker, _CodexMarker, build_codex_marker
from supercc.adapter.common.format.questionnaire import (
    _AskUserQuestionMarker,
    parse_ask_user_question,
)

__all__ = [
    # diff
    "DiffLine",
    "colorize_diff",
    "_lcs_diff",
    "_truncate_diff",
    "MAX_DIFF_LINES",
    "CONTEXT_LINES",
    # memory
    "MemoryCardMarker",
    # agent
    "_AgentCardMarker",
    "_CodexMarker",
    "build_codex_marker",
    # questionnaire
    "_AskUserQuestionMarker",
    "parse_ask_user_question",
]

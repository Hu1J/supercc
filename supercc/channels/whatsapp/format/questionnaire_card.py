"""AskUserQuestion WhatsApp 格式 — 纯文本问卷渲染。

WhatsApp 不支持交互式卡片，使用纯文本问卷格式。
"""
from __future__ import annotations

from supercc.channels.common.format.questionnaire import (
    _AskUserQuestionMarker as _BaseMarker,
    parse_ask_user_question,
    _render_question_text,
    _render_option_text,
    _Option,
    _QuestionnaireData,
)


class _AskUserQuestionMarker(_BaseMarker):
    """问卷卡片标记 — WhatsApp 平台直接使用父类纯文本渲染。"""

    def render(self) -> str:
        """构建 AskUserQuestion 的 WhatsApp 纯文本问卷。"""
        data = self.data
        if data is None:
            return f"🤔 **待确认**\n\n{self.tool_input}"

        parts = []
        header = f"🤔 **{data.header}**" if data.header else "🤔 **待您确认**"
        parts.append(header)
        parts.append("")
        parts.append(_render_question_text(data.question))
        parts.append("")

        select_label = "可多选" if data.multi_select else "单选"
        parts.append(f"**{select_label}**，请回复选项编号或内容：")
        parts.append("")

        for i, opt in enumerate(data.options, 1):
            parts.append(_render_option_text(opt, i))
            parts.append("")

        parts.append("_请直接回复选项编号（如 1）或选项内容_")
        return "\n".join(parts)
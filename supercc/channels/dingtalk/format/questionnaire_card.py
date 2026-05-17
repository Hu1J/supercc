"""AskUserQuestion 钉钉卡片构建 — 继承 common.format 基类，输出 markdown 格式。"""
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
    """问卷卡片标记 — 钉钉平台输出 markdown。"""

    def render(self) -> str:
        """构建 AskUserQuestion 的钉钉 markdown 消息。"""
        data = self.data
        if data is None:
            return f"🤔 **待确认**\n\n{self.tool_input}"

        parts = []

        # 顶部 header 标签
        tag_text = f"🤔 **{data.header}**" if data.header else "🤔 **待您确认**"
        parts.append(tag_text)
        parts.append("")

        # 问题文本
        question_md = _render_question_text(data.question)
        parts.append(question_md)
        parts.append("")

        # 分隔线
        parts.append("---")
        parts.append("")

        # 选项列表
        if data.options:
            select_label = "可多选" if data.multi_select else "单选"
            parts.append(f"**{select_label}**，请回复选项编号或内容：")
            parts.append("")

            for i, opt in enumerate(data.options, 1):
                option_md = _render_option_text(opt, i)
                parts.append(option_md)
                parts.append("")

        # 底部提示
        parts.append("_请直接回复选项编号（如 1）或选项内容_")
        return "\n".join(parts)
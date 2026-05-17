"""AskUserQuestion 问卷渲染 — WeChat 平台 markdown 版本。
WeChat 不支持 CardKit，使用纯 markdown 格式。
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
    """问卷卡片标记 — WeChat 平台输出 markdown。"""

    def render(self) -> str:
        """构建 AskUserQuestion 的 markdown 文本。"""
        data = self.data
        if data is None:
            return f"**🤔 待确认**\n\n```\n{self.tool_input}\n```"

        elements = []

        # 顶部 header 标签
        tag_text = f"**🤔 {data.header}**" if data.header else "**🤔 待您确认**"
        elements.append(tag_text)

        # 问题文本
        question_md = _render_question_text(data.question)
        elements.append(question_md)

        # 分隔线
        elements.append("---")

        # 选项列表
        if data.options:
            select_label = "可多选" if data.multi_select else "单选"
            elements.append(f"**{select_label}**，请回复选项编号或内容：")
            elements.append("")

            for i, opt in enumerate(data.options, 1):
                option_md = _render_option_text(opt, i)
                elements.append(option_md)

        # 底部提示
        elements.append("_请直接回复选项编号（如 1）或选项内容_")

        return "\n".join(elements)
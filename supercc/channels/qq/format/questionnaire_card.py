"""AskUserQuestion QQ 卡片构建 — 继承 common.format 基类，覆盖 render() 为 QQ 消息格式。"""
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
    """问卷卡片标记 — QQ 平台覆盖 render() 输出 QQ 消息格式。"""

    def render(self) -> dict:
        """构建 AskUserQuestion 的 QQ 消息。"""
        data = self.data
        if data is None:
            return {
                "content": f"🤔 **待确认**\n\n{self.tool_input}",
                "msg_type": 2,  # QQ markdown message type
            }

        parts = []

        # 顶部 header 标签
        tag_text = f"🤔 **{data.header}**" if data.header else "🤔 **待您确认**"
        parts.append(tag_text)
        parts.append("")

        # 问题文本
        question_md = _render_question_text(data.question)
        parts.append(question_md)
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

        content = "\n".join(parts)
        return {
            "content": content,
            "msg_type": 2,  # QQ markdown message type
        }
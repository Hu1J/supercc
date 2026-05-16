"""AskUserQuestion 飞书卡片构建 — 继承 common.format 基类，覆盖 render() 为 CardKit。"""
from __future__ import annotations

from supercc.adapter.common.format.questionnaire import (
    _AskUserQuestionMarker as _BaseMarker,
    parse_ask_user_question,
    _render_question_text,
    _render_option_text,
    _Option,
    _QuestionnaireData,
)


class _AskUserQuestionMarker(_BaseMarker):
    """问卷卡片标记 — 飞书平台覆盖 render() 输出 CardKit。"""

    def render(self) -> dict:
        """构建 AskUserQuestion 的飞书 Interactive Card。"""
        data = self.data
        if data is None:
            return {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "🤔 **待确认**"},
                        {"tag": "markdown", "content": f"```\n{self.tool_input}\n```"},
                    ]
                },
            }

        elements = []

        # 顶部 header 标签
        tag_text = f"🤔 **{data.header}**" if data.header else "🤔 **待您确认**"
        elements.append({"tag": "markdown", "content": tag_text})

        # 问题文本
        question_md = _render_question_text(data.question)
        elements.append({"tag": "markdown", "content": question_md})

        # 分隔线
        elements.append({"tag": "hr"})

        # 选项列表
        if data.options:
            select_label = "可多选" if data.multi_select else "单选"
            elements.append({
                "tag": "markdown",
                "content": f"**{select_label}**，请回复选项编号或内容：",
            })

            for i, opt in enumerate(data.options, 1):
                option_md = _render_option_text(opt, i)
                elements.append({"tag": "markdown", "content": option_md})
                if i < len(data.options):
                    elements.append({"tag": "hr"})

        # 底部提示
        elements.append({
            "tag": "markdown",
            "content": "_请直接回复选项编号（如 1）或选项内容_",
        })

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": elements},
        }

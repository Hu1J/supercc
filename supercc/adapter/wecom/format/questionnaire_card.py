"""AskUserQuestion 问卷卡片构建 — WeCom 使用 markdown 格式."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass


@dataclass
class _Option:
    label: str
    description: str


@dataclass
class _QuestionnaireData:
    question: str
    header: str
    options: list[_Option]
    multi_select: bool


def parse_ask_user_question(tool_input: str) -> _QuestionnaireData | None:
    """解析 AskUserQuestion tool_input JSON。"""
    try:
        data = json.loads(tool_input)
    except json.JSONDecodeError:
        return None

    questions_list = data.get("questions", [])
    if questions_list:
        q = questions_list[0] if isinstance(questions_list, list) else questions_list
    else:
        q = data

    question = q.get("question", "")
    header = q.get("header", "")
    multi_select = bool(q.get("multiSelect", False))
    raw_options = q.get("options", [])

    if not question and not raw_options:
        return None

    options = [
        _Option(label=opt.get("label", ""), description=opt.get("description", ""))
        for opt in raw_options
        if isinstance(opt, dict)
    ]
    return _QuestionnaireData(
        question=question,
        header=header,
        options=options,
        multi_select=multi_select,
    )


def format_questionnaire_card(marker: "_AskUserQuestionMarker") -> dict:
    """构建 AskUserQuestion 的 markdown 卡片（返回 dict 供渲染）。"""
    data = marker.data
    lines = []

    tag_text = f"🤔 **{data.header}**" if data.header else "🤔 **待您确认**"
    lines.append(tag_text)
    lines.append("")
    lines.append(data.question)
    lines.append("")
    lines.append("---")
    lines.append("")

    select_label = "可多选" if data.multi_select else "单选"
    lines.append(f"**{select_label}**，请回复选项编号或内容：")
    lines.append("")

    for i, opt in enumerate(data.options, 1):
        lines.append(f"**{i}. {opt.label}**")
        if opt.description:
            lines.append(opt.description)
        lines.append("")

    lines.append("_请直接回复选项编号（如 1）或选项内容_")

    return {
        "type": "markdown",
        "content": "\n".join(lines),
    }


class _AskUserQuestionMarker:
    """通知 message_handler 此工具调用需要渲染问卷卡片。"""
    __slots__ = ("tool_name", "tool_input", "data")

    def __init__(self, tool_name: str, tool_input: str):
        self.tool_name = tool_name
        self.tool_input = tool_input
        parsed = parse_ask_user_question(tool_input)
        self.data: _QuestionnaireData | None = parsed

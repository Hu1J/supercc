"""AskUserQuestion 问卷渲染 — 平台无关基类。"""
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
    """解析 AskUserQuestion tool_input JSON。

    支持两种格式：
    - 单问题格式：{"question": "...", "options": [...], "header": "...", "multiSelect": false}
    - 多问题格式：{"questions": [{"question": "...", "options": [...], "header": "...", "multiSelect": false}]}
    """
    try:
        data = json.loads(tool_input)
    except (json.JSONDecodeError, TypeError):
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


def _render_question_text(question: str) -> str:
    """渲染问题文本，保留 markdown 格式。"""
    text = re.sub(r"\n{3,}", "\n\n", question)
    return text.strip()


def _render_option_text(option: _Option, index: int) -> str:
    """将单个选项渲染为加粗标签 + 描述。"""
    label = f"**{index}. {option.label}**"
    if option.description:
        return f"{label}\n{option.description}"
    return label


class _AskUserQuestionMarker:
    """问卷卡片标记，平台无关实现 render() 为纯 markdown。

    各平台可继承并覆盖 render() 输出平台原生格式。
    """
    __slots__ = ("tool_name", "tool_input", "data")

    def __init__(self, tool_name: str, tool_input: str):
        self.tool_name = tool_name
        self.tool_input = tool_input
        parsed = parse_ask_user_question(tool_input)
        self.data: _QuestionnaireData | None = parsed

    def render(self) -> str:
        """渲染为纯 markdown（平台无关兜底实现）。"""
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

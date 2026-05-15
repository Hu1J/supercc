"""AskUserQuestion 渲染 — WeCom markdown 版本。"""
from __future__ import annotations
import json


def parse_ask_user_question(tool_input: str):
    """解析 AskUserQuestion tool_input JSON。"""
    try:
        data = json.loads(tool_input)
    except json.JSONDecodeError:
        return None

    questions_list = data.get("questions", [])
    if questions_list:
        q = questions_list[0] if isinstance(questions_list, list) else questions_list
        header = q.get("header", "问题")
        question = q.get("question", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)
        return (header, question, options, multi_select)
    return None


def format_questionnaire_markdown(tool_input: str) -> str:
    """将 AskUserQuestion 格式化为 WeCom markdown 列表。"""
    result = parse_ask_user_question(tool_input)
    if not result:
        return tool_input

    header, question, options, multi_select = result
    suffix = "（可多选）" if multi_select else "（单选）"

    lines = [f"**{header}{suffix}**", "", question, ""]
    for i, opt in enumerate(options, 1):
        if isinstance(opt, dict):
            label = opt.get("label", str(opt))
        else:
            label = str(opt)
        lines.append(f"{i}. {label}")
    lines.append("")
    lines.append("请回复选项编号。")
    return "\n".join(lines)

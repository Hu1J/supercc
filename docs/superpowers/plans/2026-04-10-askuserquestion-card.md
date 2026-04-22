# AskUserQuestion 飞书卡片实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拦截 Claude Code 自带的 `AskUserQuestion` 工具调用，用精美的飞书 Interactive Card 替代默认的纯文本渲染，展示问题和选项。

**Architecture:**
- 在 `ReplyFormatter.format_tool_call` 中新增 `AskUserQuestion` 分支，构建 `_AskUserQuestionMarker`
- 新建 `cc_feishu_bridge/format/questionnaire_card.py`，包含卡片构建函数和 marker 类
- 在 `MessageHandler.stream_callback` 的 `_MemoryCardMarker` 处理后新增 `elif isinstance(result, _AskUserQuestionMarker)` 分支，调用 `feishu.send_interactive` 发送卡片

**Tech Stack:** Feishu Interactive Card (schema 2.0), lark-oapi, Python

---

## Task 1: 新建 questionnaire_card.py — 卡片构建逻辑

**Files:**
- Create: `cc_feishu_bridge/format/questionnaire_card.py`
- Test: `tests/test_questionnaire_card.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_questionnaire_card.py
import pytest
from cc_feishu_bridge.format.questionnaire_card import (
    parse_ask_user_question,
    format_questionnaire_card,
    _AskUserQuestionMarker,
)

def test_parse_basic():
    tool_input = '{"question":"你的用户从哪里来？","header":"用户来源","options":[{"label":"私域流量","description":"已有公众号或社群"},{"label":"内容引流","description":"微博/小红书/抖音发内容导流"}],"multiSelect":false}'
    result = parse_ask_user_question(tool_input)
    assert result.question == "你的用户从哪里来？"
    assert result.header == "用户来源"
    assert len(result.options) == 2
    assert result.options[0].label == "私域流量"
    assert result.multi_select is False

def test_format_card_structure():
    tool_input = '{"question":"你的用户从哪里来？","header":"用户来源","options":[{"label":"私域流量","description":"已有公众号或社群"},{"label":"内容引流","description":"微博/小红书/抖音发内容导流"}],"multiSelect":false}'
    marker = _AskUserQuestionMarker("AskUserQuestion", tool_input)
    card = format_questionnaire_card(marker)
    assert card["schema"] == "2.0"
    assert card["config"]["wide_screen_mode"] is True
    assert "body" in card
    assert "elements" in card["body"]

def test_invalid_json_returns_none():
    result = parse_ask_user_question("not json")
    assert result is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_questionnaire_card.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cc_feishu_bridge.format.questionnaire_card'`

- [ ] **Step 3: 实现 questionnaire_card.py**

```python
"""AskUserQuestion 飞书卡片构建。"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass

# 飞书卡片 color 常量
COLOR_PRIMARY = "blue"
COLOR_GREY = "grey"
COLOR_RED = "red"
COLOR_GREEN = "green"


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

    question = data.get("question", "")
    header = data.get("header", "")
    multi_select = bool(data.get("multiSelect", False))
    raw_options = data.get("options", [])

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
    # 保留加粗、换行，折叠多余空行
    text = re.sub(r"\n{3,}", "\n\n", question)
    return text.strip()


def _render_option_text(option: _Option, index: int) -> str:
    """将单个选项渲染为加粗标签 + 描述。"""
    label = f"**{index}. {option.label}**"
    if option.description:
        return f"{label}\n{option.description}"
    return label


def format_questionnaire_card(marker: "_AskUserQuestionMarker") -> dict:
    """构建 AskUserQuestion 的飞书 Interactive Card。"""
    data = marker.data
    elements = []

    # 顶部 header 标签（如果有）
    if data.header:
        elements.append({
            "tag": "tag",
            "text": f"📋 {data.header}",
            "color": "grey",
        })

    # 问题文本（markdown 渲染）
    question_md = _render_question_text(data.question)
    elements.append({
        "tag": "markdown",
        "content": question_md,
    })

    # 分隔线
    elements.append({"tag": "hr"})

    # 选项列表
    if data.options:
        # 单选/多选标签
        select_label = "可多选" if data.multi_select else "单选"
        elements.append({
            "tag": "markdown",
            "content": f"**{select_label}**，请回复选项编号或内容：",
        })

        for i, opt in enumerate(data.options, 1):
            option_md = _render_option_text(opt, i)
            elements.append({
                "tag": "markdown",
                "content": option_md,
            })
            if i < len(data.options):
                elements.append({"tag": "hr"})

    # 底部提示
    elements.append({
        "tag": "markdown",
        "content": "_请直接回复选项编号（如 1）或选项内容_",
    })

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "body": {
            "elements": elements,
        },
    }


class _AskUserQuestionMarker:
    """通知 message_handler 此工具调用需要渲染问卷卡片。"""
    __slots__ = ("tool_name", "tool_input", "data")

    def __init__(self, tool_name: str, tool_input: str):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        parsed = parse_ask_user_question(tool_input)
        self.data: _QuestionnaireData | None = parsed
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_questionnaire_card.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_feishu_bridge/format/questionnaire_card.py tests/test_questionnaire_card.py
git commit -m "feat: add AskUserQuestion card formatting

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: 在 ReplyFormatter 中接入 AskUserQuestion 检测

**Files:**
- Modify: `cc_feishu_bridge/format/reply_formatter.py`

- [ ] **Step 1: 添加导入**

在文件顶部 import 区域添加：
```python
from cc_feishu_bridge.format.questionnaire_card import _AskUserQuestionMarker, parse_ask_user_question
```

- [ ] **Step 2: 在 format_tool_call 中新增 AskUserQuestion 分支**

在 `_format_todowrite_tool` 之后、`# Memory MCP tools` 注释之前添加：

```python
        # AskUserQuestion → 精美问卷卡片
        elif tool_name == "AskUserQuestion":
            marker = _AskUserQuestionMarker(tool_name, tool_input)
            if marker.data is not None:
                return marker
            # 解析失败，降级为普通文本
            return f"🤖 **{tool_name}**\n`{tool_input}`"
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/ -v -k "questionnaire or formatter" --tb=short`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add cc_feishu_bridge/format/reply_formatter.py
git commit -m "feat: wire AskUserQuestion into ReplyFormatter

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: 在 MessageHandler 中渲染 AskUserQuestion 卡片

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py:18` (import)
- Modify: `cc_feishu_bridge/feishu/message_handler.py:839` (stream_callback)

- [ ] **Step 1: 更新导入**

在第19行附近（`_MemoryCardMarker` 导入后）添加：
```python
from cc_feishu_bridge.format.questionnaire_card import _AskUserQuestionMarker
```

- [ ] **Step 2: 在 stream_callback 中新增分支**

在 `_MemoryCardMarker` 处理块之后、`else` 分支之前添加：

```python
                    elif isinstance(result, _AskUserQuestionMarker):
                        # AskUserQuestion → 精美飞书卡片
                        if result.data is not None:
                            from cc_feishu_bridge.format.questionnaire_card import format_questionnaire_card
                            card = format_questionnaire_card(result)
                            try:
                                await self.feishu.send_edit_diff_card(
                                    message.chat_id, card, message.message_id, log_reply=False
                                )
                            except Exception:
                                # 卡片发送失败，降级为带图标的纯文本
                                await self._safe_send(
                                    message.chat_id, message.message_id,
                                    f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                    log_reply=False,
                                )
                        else:
                            await self._safe_send(
                                message.chat_id, message.message_id,
                                f"🤖 **{result.tool_name}**\n`{result.tool_input[:500]}`",
                                log_reply=False,
                            )
```

- [ ] **Step 3: 验证语法**

Run: `python -m py_compile cc_feishu_bridge/feishu/message_handler.py && echo "OK"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: render AskUserQuestion as Feishu interactive card

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 4: 端到端验证

**Files:**
- 无文件变更 — 手动测试

- [ ] **Step 1: 运行全量测试**

Run: `pytest tests/ -v --tb=short 2>&1 | tail -20`
Expected: 全部 PASS

- [ ] **Step 2: 检查 import 无循环依赖**

Run: `python -c "from cc_feishu_bridge.format.reply_formatter import ReplyFormatter; from cc_feishu_bridge.format.questionnaire_card import _AskUserQuestionMarker; print('OK')"`
Expected: OK

---

## Task 5: 发版

- [ ] **Step 1: 更新 CHANGELOG.md**

在 `[Unreleased]` 或 `[0.3.15]` 下添加：
```markdown
### Added
- **AskUserQuestion 飞书卡片**：Claude Code 的 `AskUserQuestion` 工具调用现在以精美的飞书 Interactive Card 展示，包含问题标题、选项列表和交互提示
```

- [ ] **Step 2: 更新版本号**

修改 `pyproject.toml` version 为 `0.3.15`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "release: bump version to 0.3.15

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

- [ ] **Step 4: 打标签并推送**

```bash
git tag -a v0.3.15 -m "v0.3.15: AskUserQuestion card layout"
git push origin feat/tool-card
git push origin feat/tool-card --tags
```

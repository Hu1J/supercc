# Edit + Write 彩色 Diff 卡片实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Goal:** 飞书消息中展示红绿灰彩色 diff 卡片，Edit 工具用 LCS 对齐，Write 工具输出全量绿色新增
>
> **Architecture:** 新增 `edit_diff.py` 模块包含 LCS 算法和卡片构建；修改 `reply_formatter.py` 对 Edit/Write 返回特殊 marker；修改 `message_handler.py` 在 stream callback 里拦截并发送彩色卡片。
>
> **Tech Stack:** 纯 Python 标准库，无外部依赖

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `cc_feishu_bridge/format/edit_diff.py` | 新增 | LCS diff 算法 + 卡片 JSON 构建 |
| `cc_feishu_bridge/format/__init__.py` | 修改 | 导出 `colorize_diff`, `format_tool_card` |
| `cc_feishu_bridge/format/reply_formatter.py` | 修改 | `format_tool_call()` 对 Edit/Write 返回 `_DiffMarker` |
| `cc_feishu_bridge/feishu/message_handler.py` | 修改 | `stream_callback` 识别 `_DiffMarker` → 调用 `send_edit_diff_card()` |
| `cc_feishu_bridge/feishu/client.py` | 修改 | 新增 `send_edit_diff_card()` 方法 |
| `tests/test_edit_diff.py` | 新增 | LCS 算法 + 卡片构建测试 |

---

## Task 1: 创建 `edit_diff.py` — LCS 算法

**Files:**
- Create: `cc_feishu_bridge/format/edit_diff.py`

```python
"""彩色 diff 渲染 — Edit/Write 工具专用。"""
from __future__ import annotations
import json

# 飞书 plain_text 支持的颜色
COLOR_RED = "red"
COLOR_GREEN = "green"    # 注：浅色主题下偏淡，可调整
COLOR_GREY = "grey"
COLOR_BLUE = "blue"
COLOR_DEFAULT = "default"

MAX_DIFF_LINES = 50       # 超过此行数截断
CONTEXT_LINES = 3        # 截断时保留首尾上下文行数
MAX_CARD_LINES = 30      # 单次卡片最大行数


class DiffLine:
    """一行 diff 结果。"""
    __slots__ = ("type", "content")   # type: "deletion" | "insertion" | "context"

    def __init__(self, type: str, content: str):
        self.type = type
        self.content = content

    def color(self) -> str:
        if self.type == "deletion":
            return COLOR_RED
        elif self.type == "insertion":
            return COLOR_GREEN
        return COLOR_GREY

    def prefix(self) -> str:
        if self.type == "deletion":
            return "- "
        elif self.type == "insertion":
            return "+ "
        return "  "


def colorize_diff(old_string: str, new_string: str) -> list[DiffLine]:
    """对 old_string 和 new_string 做行级 LCS，返回带类型的行列表。"""
    if not old_string and not new_string:
        return []
    old_lines = old_string.splitlines()
    new_lines = new_string.splitlines()
    diff = _lcs_diff(old_lines, new_lines)

    # 截断：超过 MAX_DIFF_LINES 时，首尾各保留 CONTEXT_LINES 行上下文
    if len(diff) > MAX_DIFF_LINES:
        diff = _truncate_diff(diff)

    return diff


def _lcs_diff(old_lines: list[str], new_lines: list[str]) -> list[DiffLine]:
    """计算 LCS 并返回行级 diff。"""
    m, n = len(old_lines), len(new_lines)
    # LCS 长度矩阵
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if old_lines[i - 1] == new_lines[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # 回溯找 diff
    result = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and old_lines[i - 1] == new_lines[j - 1]:
            result.append(DiffLine("context", old_lines[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            result.append(DiffLine("insertion", new_lines[j - 1]))
            j -= 1
        else:
            result.append(DiffLine("deletion", old_lines[i - 1]))
            i -= 1

    result.reverse()
    return result


def _truncate_diff(diff: list[DiffLine]) -> list[DiffLine]:
    """截断过长的 diff，保留首尾上下文。"""
    # 找第一个和最后一个变化行（不是 context 的行）
    first_change = next((i for i, d in enumerate(diff) if d.type != "context"), 0)
    last_change = next((len(diff) - 1 - i for i, d in enumerate(reversed(diff)) if d.type != "context"), len(diff) - 1)

    head = diff[:first_change]
    tail = diff[last_change + 1:]
    middle = diff[first_change:last_change + 1]

    # 保留前 CONTEXT_LINES 行上下文
    keep_head = diff[:CONTEXT_LINES]
    keep_tail = diff[-CONTEXT_LINES:] if len(diff) >= CONTEXT_LINES else diff

    return keep_head + [DiffLine("context", "...")] + keep_tail


def format_edit_card(file_path: str, diff_lines: list[DiffLine]) -> dict:
    """构建 Edit 工具的飞书彩色 diff 卡片。"""
    header_title = f"✏️ Edit — `{file_path}`"
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": header_title,
                "text_color": COLOR_BLUE,
            }
        },
        {
            "tag": "div",
            "fields": [
                {
                    "text": {
                        "tag": "plain_text",
                        "content": _render_diff_lines(diff_lines),
                        "text_color": COLOR_DEFAULT,
                    }
                }
            ],
            "background_color": "#1e1e1e",
        }
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def format_write_card(file_path: str, content_lines: list[str]) -> dict:
    """构建 Write 工具的飞书全量绿色卡片。"""
    header_title = f"✏️ Write — `{file_path}`"
    diff_lines = [DiffLine("insertion", line) for line in content_lines]
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": header_title,
                "text_color": COLOR_BLUE,
            }
        },
        {
            "tag": "div",
            "fields": [
                {
                    "text": {
                        "tag": "plain_text",
                        "content": _render_diff_lines(diff_lines),
                        "text_color": COLOR_DEFAULT,
                    }
                }
            ],
            "background_color": "#1e1e1e",
        }
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


def _render_diff_lines(diff_lines: list[DiffLine]) -> str:
    """将 DiffLine 列表渲染为带前缀的颜色文本（用于 plain_text）。"""
    # 飞书 plain_text 不支持多色，需要分行
    # 返回格式：每行 "[-/+//  ]content"，颜色由调用方在元素级别控制
    parts = []
    for d in diff_lines:
        parts.append(f"{d.prefix()}{d.content}")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# 供 reply_formatter 使用的 marker
# ----------------------------------------------------------------------
class _DiffMarker:
    """通知 message_handler 此工具调用需要渲染彩色 diff 卡片。"""
    __slots__ = ("tool_name", "tool_input", "card")

    def __init__(self, tool_name: str, tool_input: str, card: dict):
        self.tool_name = tool_name
        self.tool_input = tool_input  # 原始 JSON 字符串
        self.card = card              # 预构建的飞书卡片 JSON


def build_edit_marker(tool_input_json: str) -> _DiffMarker:
    """从 Edit 工具的 tool_input JSON 构建 marker。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    old_str = data.get("old_string", "")
    new_str = data.get("new_string", "")
    diff = colorize_diff(old_str, new_str)
    card = format_edit_card(file_path, diff)
    return _DiffMarker("Edit", tool_input_json, card)


def build_write_marker(tool_input_json: str) -> _DiffMarker:
    """从 Write 工具的 tool_input JSON 构建 marker。"""
    data = json.loads(tool_input_json)
    file_path = data.get("file_path", "unknown")
    content = data.get("content", "")
    lines = content.splitlines()
    # Write 过长时分块：每块 MAX_CARD_LINES 行
    if len(lines) <= MAX_CARD_LINES:
        return _DiffMarker("Write", tool_input_json, format_write_card(file_path, lines))
    # 多块：返回多个 marker
    chunks = [lines[i:i + MAX_CARD_LINES] for i in range(0, len(lines), MAX_CARD_LINES)]
    return [_DiffMarker("Write", tool_input_json, format_write_card(file_path, chunk)) for chunk in chunks]
```

- [ ] **Step 1: 创建 `edit_diff.py` 并写入上述代码**

- [ ] **Step 2: 运行测试确认模块可导入**

```bash
python3 -c "from cc_feishu_bridge.format.edit_diff import colorize_diff, build_edit_marker; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/format/edit_diff.py && git commit -m "feat: add edit_diff module with LCS algorithm and card builder"
```

---

## Task 2: 修改 `reply_formatter.py` — 返回 `_DiffMarker`

**Files:**
- Modify: `cc_feishu_bridge/format/reply_formatter.py:167-180`

```python
# 在文件顶部新增 import
from cc_feishu_bridge.format.edit_diff import build_edit_marker, build_write_marker

# 修改 format_tool_call 方法（第167行）
def format_tool_call(self, tool_name: str, tool_input: str | None = None) -> str | _DiffMarker:
    """Format a tool call notification for the user.

    Returns _DiffMarker for Edit/Write tools (to trigger colored card rendering),
    or a plain string for all other tools.
    """
    if tool_input is None:
        tool_input = ""

    # Edit / Write → 彩色 diff 卡片
    if tool_name == "Edit":
        if tool_input.strip():
            try:
                return build_edit_marker(tool_input)
            except (json.JSONDecodeError, KeyError):
                pass  # 降级到 backtick 格式
    elif tool_name == "Write":
        if tool_input.strip():
            try:
                return build_write_marker(tool_input)
            except (json.JSONDecodeError, KeyError):
                pass  # 降级到 backtick 格式

    # 其他工具 → backtick 格式（原有逻辑）
    icon = self.tool_icons.get(tool_name, "🤖")
    msg = f"{icon} **{tool_name}**"
    if tool_input:
        if len(tool_input) <= FEISHU_MAX_MESSAGE_LENGTH - len(msg) - 5:
            msg += f"\n`{tool_input}`"
        else:
            chunks = self.split_messages(tool_input)
            for chunk in chunks:
                msg += f"\n`{chunk}`"
    return msg
```

- [ ] **Step 1: 在 `reply_formatter.py` 文件开头添加 import（第 5 行附近）：**

```python
import json
from cc_feishu_bridge.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker
```

- [ ] **Step 2: 修改 `format_tool_call` 方法（第167-180行）**

用上面的完整实现替换原有 `format_tool_call` 方法。

- [ ] **Step 3: 运行测试**

```bash
python3 -c "from cc_feishu_bridge.format.reply_formatter import ReplyFormatter; f = ReplyFormatter(); r = f.format_tool_call('Edit', '{\"file_path\":\"/tmp/a.txt\",\"old_string\":\"foo\",\"new_string\":\"bar\"}'); print(type(r).__name__, hasattr(r, 'card'))"
```
Expected: `type: _DiffMarker, has card: True`

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/format/reply_formatter.py && git commit -m "feat: format_tool_call returns _DiffMarker for Edit/Write tools"
```

---

## Task 3: 修改 `client.py` — 新增 `send_edit_diff_card()` 方法

**Files:**
- Modify: `cc_feishu_bridge/feishu/client.py`（在 `send_interactive_reply` 方法后插入）

在 `send_interactive_reply` 方法末尾 `return response.data.message_id` 之后，添加：

```python
async def send_edit_diff_card(
    self,
    chat_id: str,
    card: dict,
    reply_to_message_id: str,
    log_reply: bool = True,
) -> str:
    """Send a pre-built colored diff card as a threaded reply."""
    msg_id = await self.send_interactive(chat_id, card, reply_to_message_id)
    if log_reply:
        logger.info(f"Replied diff card to {reply_to_message_id} in chat {chat_id}: {msg_id}")
    return msg_id
```

- [ ] **Step 1: 找到 `send_interactive_reply` 末尾（大约 client.py 第471行）**

在 `return response.data.message_id` 之后插入上述新方法。

- [ ] **Step 2: 验证语法**

```bash
python3 -c "from cc_feishu_bridge.feishu.client import FeishuClient; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/feishu/client.py && git commit -m "feat: add send_edit_diff_card() method"
```

---

## Task 4: 修改 `message_handler.py` — stream callback 拦截 `_DiffMarker`

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py:343-351`

```python
# 在文件顶部 import 区域添加
from cc_feishu_bridge.format.edit_diff import _DiffMarker

# 修改 stream_callback（第343-351行）
async def stream_callback(claude_msg):
    if claude_msg.tool_name:
        await accumulator.flush()
        result = self.formatter.format_tool_call(
            claude_msg.tool_name,
            claude_msg.tool_input,
        )
        logger.info(f"[stream] tool: {claude_msg.tool_name} | input: {claude_msg.tool_input}")

        # _DiffMarker → 彩色卡片；其他 → backtick 格式
        if isinstance(result, _DiffMarker):
            cards = result.card if isinstance(result.card, list) else [result.card]
            for card in cards:
                await self.feishu.send_edit_diff_card(
                    message.chat_id, card, message.message_id, log_reply=False
                )
        else:
            await self._safe_send(message.chat_id, message.message_id, result, log_reply=False)
    elif claude_msg.content:
        logger.info(f"[stream] text: {claude_msg.content[:100]}")
        await accumulator.add_text(claude_msg.content)
```

- [ ] **Step 1: 在 `message_handler.py` 顶部 import 区域（第 1-18 行附近）添加**

```python
from cc_feishu_bridge.format.edit_diff import _DiffMarker
```

- [ ] **Step 2: 修改 `stream_callback`（第343-351行）**

用上面的完整实现替换原有的 `stream_callback`。

- [ ] **Step 3: 验证**

```bash
python3 -c "from cc_feishu_bridge.feishu.message_handler import MessageHandler; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py && git commit -m "feat: stream_callback sends colored diff card for Edit/Write tools"
```

---

## Task 5: 测试

**Files:**
- Create: `tests/test_edit_diff.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.format.edit_diff import (
    colorize_diff,
    _lcs_diff,
    _truncate_diff,
    format_edit_card,
    format_write_card,
    build_edit_marker,
    build_write_marker,
    _DiffMarker,
    COLOR_RED, COLOR_GREEN, COLOR_GREY,
    MAX_DIFF_LINES,
)


class TestLCS:
    def test_no_change(self):
        diff = colorize_diff("foo\nbar", "foo\nbar")
        assert all(d.type == "context" for d in diff)
        assert len(diff) == 2

    def test_add_lines(self):
        diff = colorize_diff("foo", "foo\nbar")
        types = [d.type for d in diff]
        assert types == ["context", "insertion"]

    def test_remove_lines(self):
        diff = colorize_diff("foo\nbar", "foo")
        types = [d.type for d in diff]
        assert types == ["context", "deletion"]

    def test_replace_line(self):
        diff = colorize_diff("foo", "bar")
        types = [d.type for d in diff]
        assert types == ["deletion", "insertion"]

    def test_empty_old(self):
        diff = colorize_diff("", "foo\nbar")
        assert all(d.type == "insertion" for d in diff)

    def test_empty_new(self):
        diff = colorize_diff("foo\nbar", "")
        assert all(d.type == "deletion" for d in diff)


class TestTruncate:
    def test_under_limit(self):
        lines = [object()] * 30  # dummy objects
        diff = _truncate_diff(lines)
        assert len(diff) == 30

    def test_over_limit_truncates(self):
        lines = [object() for _ in range(60)]
        diff = _truncate_diff(lines)
        assert len(diff) < 60


class TestFormatCard:
    def test_edit_card_structure(self):
        diff = colorize_diff("foo", "bar")
        card = format_edit_card("/tmp/test.txt", diff)
        assert card["schema"] == "2.0"
        assert "body" in card
        # header element
        elements = card["body"]["elements"]
        assert elements[0]["tag"] == "div"
        assert "plain_text" in elements[0]["text"]

    def test_write_card_structure(self):
        card = format_write_card("/tmp/test.txt", ["line1", "line2"])
        assert card["schema"] == "2.0"
        assert "body" in card


class TestBuildMarker:
    def test_edit_marker(self):
        inp = '{"file_path": "/tmp/a.txt", "old_string": "foo", "new_string": "bar"}'
        marker = build_edit_marker(inp)
        assert isinstance(marker, _DiffMarker)
        assert marker.tool_name == "Edit"
        assert "card" in marker.__dict__

    def test_edit_invalid_json_falls_back(self):
        from cc_feishu_bridge.format.reply_formatter import ReplyFormatter
        f = ReplyFormatter()
        result = f.format_tool_call("Edit", "not json")
        assert isinstance(result, str)  # falls back to string

    def test_write_marker(self):
        inp = '{"file_path": "/tmp/a.txt", "content": "hello\\nworld"}'
        marker = build_write_marker(inp)
        assert isinstance(marker, _DiffMarker)
        assert marker.tool_name == "Write"
```

- [ ] **Step 1: 创建 `tests/test_edit_diff.py` 并写入上述测试代码**

- [ ] **Step 2: 运行测试**

```bash
python3 -m pytest tests/test_edit_diff.py -v
```
Expected: 全部 PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_edit_diff.py && git commit -m "test: add edit_diff tests"
```

---

## Task 6: 集成测试（手动）

构建并启动 bridge，用 Claude Code 编辑一个文件，观察飞书里的 Edit 工具调用是否显示彩色卡片。

```bash
python3 -m build 2>&1 | tail -3
python3 build_cli.py 2>&1 | tail -3
```

- [ ] 手动验证：发送编辑请求，观察飞书消息是否显示红绿 diff 卡片

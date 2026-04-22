# /git 命令 + TodoWrite 卡片实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `/git` 命令展示 git status 和最近 5 次提交；拦截 TodoWrite 工具调用渲染为精美卡片。

**Architecture:** `/git` 在 `message_handler._handle_command` 新增分支，直接执行 git 命令后渲染 markdown 发卡；TodoWrite 在 `reply_formatter.format_tool_call` 新增分支，解析 todos 数组渲染 markdown 表格，两个功能均复用现有的 `send_interactive_reply` 发卡链路。

**Tech Stack:** Python 标准库 subprocess 执行 git 命令，飞书 lark_md 语法渲染彩色文字和表格。

---

## 文件概览

| 文件 | 改动 |
|------|------|
| `cc_feishu_bridge/feishu/message_handler.py` | 新增 `/git` 命令分支 |
| `cc_feishu_bridge/format/reply_formatter.py` | 新增 TodoWrite 格式化方法 |
| `tests/test_reply_formatter.py` | 新增 TodoWrite 格式化测试 |
| `tests/test_message_handler.py` | 新增 `/git` 命令测试 |

---

## Task 1: TodoWrite 格式化

**Files:**
- Modify: `cc_feishu_bridge/format/reply_formatter.py` — 在 `format_tool_call` 新增 `tool_name == "TodoWrite"` 分支
- Test: `tests/test_reply_formatter.py` — 新增 3 个测试用例

### 实现

在 `format_tool_call` 方法中，`tool_name == "Bash"` 分支后新增：

```python
elif tool_name == "TodoWrite":
    return self._format_todowrite_tool(tool_input)
```

新增方法 `_format_todowrite_tool`：

```python
def _format_todowrite_tool(self, tool_input: str) -> str:
    """Format TodoWrite tool call as a markdown table."""
    try:
        data = json.loads(tool_input)
        todos = data.get("todos", [])
    except json.JSONDecodeError:
        todos = []

    if not todos:
        return "✅ 所有任务已完成！"

    status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
    rows = ["| 状态 | 待办事项 | 当前动作 |", "|------|----------|----------|"]
    for t in todos:
        icon = status_icon.get(t.get("status", "pending"), "⬜")
        content = t.get("content", "")
        active = t.get("activeForm", "")
        rows.append(f"| {icon} | {content} | {active} |")

    return "📋 Todo List\n\n" + "\n".join(rows)
```

---

## Task 2: TodoWrite 测试

- [ ] **Step 1: 写 TodoWrite 测试**

在 `tests/test_reply_formatter.py` 新增：

```python
def test_format_todowrite_with_items(formatter):
    import json
    tool_input = json.dumps({
        "todos": [
            {"content": "Write tests", "status": "completed", "activeForm": "Writing tests"},
            {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"},
            {"content": "Deploy", "status": "pending", "activeForm": "Deploying"},
        ]
    })
    result = formatter.format_tool_call("TodoWrite", tool_input)
    assert "📋 Todo List" in result
    assert "| ⬜ | Write tests | Writing tests |" in result
    assert "| 🔄 | Fix bug | Fixing bug |" in result
    assert "| ✅ | Deploy | Deploying |" in result
    assert "✅" in result  # table header has check icon
    assert "activeForm" not in result  # actual key name not leaked


def test_format_todowrite_empty(formatter):
    import json
    tool_input = json.dumps({"todos": []})
    result = formatter.format_tool_call("TodoWrite", tool_input)
    assert result == "✅ 所有任务已完成！"


def test_format_todowrite_invalid_json(formatter):
    result = formatter.format_tool_call("TodoWrite", "not json")
    assert result == "✅ 所有任务已完成！"
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/test_reply_formatter.py -v`
Expected: 3 个新测试 PASS，现有测试不受影响

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/format/reply_formatter.py tests/test_reply_formatter.py
git commit -m "feat: add TodoWrite card formatting with status icons and table"
```

---

## Task 3: /git 命令实现

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py` — 在 `_handle_command` 新增 `/git` 分支
- Test: `tests/test_message_handler.py` — 新增 `/git` 命令测试

### 实现

在 `_handle_command` 方法中，`cmd == "/help"` 分支后新增：

```python
elif cmd == "/git":
    return await self._handle_git(message)
```

新增方法：

```python
async def _handle_git(self, message: IncomingMessage) -> HandlerResult:
    """执行 git status 和 log，返回精美卡片。"""
    import subprocess

    def run_git(args: list[str], cwd: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True, text=True, timeout=10,
                cwd=cwd or os.getcwd()
            )
            return result.stdout.strip()
        except Exception:
            return ""

    # 当前分支
    branch = run_git(["branch", "--show-current"])
    if not branch:
        branch = "(无分支)"

    # 变更文件
    status_output = run_git(["status", "--porcelain"])

    # 最近 5 次提交: 时间 + hash(7位) + 描述
    log_lines = run_git(["log", "--format=%ci %h %s", "-5"]).splitlines()

    # 渲染 markdown
    card_lines = ["📊 **Git Status**", "", f"🟢 **{branch}**", "", "📝 **变更文件**"]

    if status_output:
        status_color = {
            "M": "red", "D": "red", "A": "green",
            "R": "yellow", "U": "orange", "?": "grey",
        }
        for line in status_output.splitlines():
            index, worktree = line[:2], line[3:]
            idx_char = index[0] if index[0] not in (" ", "?"):
            wt_char = index[1] if len(index) > 1 and index[1] not in (" ", "?"):
            char = wt_char if idx_char == " " else idx_char
            color = status_color.get(char, "grey")
            card_lines.append(f"<font color='{color}'>{line[:2]}</font> {line[3:]}")

        card_lines.append("")
        card_lines.append("📋 **最近提交**")
        card_lines.append("")
        card_lines.append("| 时间 | Hash | 描述 |")
        card_lines.append("|------|------|------|")
        for log_line in log_lines:
            # 格式: "2026-04-04 12:00:00 +0800 abc1234 fix: message"
            parts = log_line.split(" ", 3)
            if len(parts) >= 4:
                dt = parts[0] + " " + parts[1][:5]  # "2026-04-04 12:00"
                h = parts[2]
                msg = parts[3]
                card_lines.append(f"| {dt} | `{h}` | {msg} |")
    else:
        card_lines.append("✅ **工作区干净，无待提交变更**")

    card_body = "\n".join(card_lines)

    try:
        await self.feishu.send_interactive_reply(
            message.chat_id, card_body, message.message_id, log_reply=True
        )
    except Exception:
        await self._safe_send(message.chat_id, message.message_id, card_body)

    return HandlerResult(success=True)
```

**注意：** 需要在 `message_handler.py` 顶部确保 `import os` 已存在（检查一下）。

---

## Task 4: /git 命令测试

- [ ] **Step 1: 写 /git 命令测试**

在 `tests/test_message_handler.py` 新增（需要确认文件存在，路径）：
如果文件不存在或为空，创建基础测试：

```python
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cc_feishu_bridge.feishu.message_handler import _is_command

def test_git_command():
    """验证 /git 被识别为命令（不触发 Claude）。"""
    assert _is_command("/git") == True
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/test_message_handler.py -v`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py tests/test_message_handler.py
git commit -m "feat: add /git command for git status and recent commits card"
```

---

## Task 5: 手动验证

- [ ] **Step 1: 发送 /git 到飞书，验证卡片样式**
- [ ] **Step 2: 触发一次 TodoWrite 工具调用，验证卡片样式**
- [ ] **Step 3: 如有问题，修复并重测**

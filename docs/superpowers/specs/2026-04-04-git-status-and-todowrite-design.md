# 设计文档：/git 命令 + TodoWrite 精美卡片

## 概述

新增两个功能：
1. `/git` — 飞书命令，直接展示当前项目 git status 和最近 5 次提交
2. TodoWrite 拦截 — Claude 发出 TodoWrite 工具调用时，自动渲染为精美卡片

## 功能一：/git 命令

### 入口
飞书消息发送 `/git`，跳过 Claude，直接执行 git 命令，返回精美飞书卡片。

### 卡片结构（markdown）

```
📊 Git Status

🟢 <分支名>

📝 变更文件
<状态> <文件路径>
...

📋 最近提交

| 时间 | Hash | 描述 |
|------|------|------|
| 2026-04-04 12:00 | abc1234 | fix: resolve bug |
| ... | ... | ... |
```

### 变更文件格式

| 状态字符 | 含义 | 颜色 |
|----------|------|------|
| `M` | 修改 | 红色 `<font color='red'>` |
| `D` | 删除 | 红色 `<font color='red'>` |
| `A` | 新增/已暂存 | 绿色 `<font color='green'>` |
| `R` | 重命名 | 黄色 `<font color='yellow'>` |
| `U` | 未合并冲突 | 橙色 `<font color='orange'>` |
| `??` | 未跟踪文件 | 灰色 `<font color='grey'>` |

### 无变更时

```
✅ 工作区干净，无待提交变更
```

### 实现方式

在 `message_handler.py` 的 `_handle_command` 中新增 `/git` 分支：
1. 执行 `git branch --show-current` 获取当前分支
2. 执行 `git status --porcelain` 获取变更文件列表
3. 执行 `git log --oneline -5` 获取最近 5 次提交
4. 解析输出，渲染 markdown
5. 调用 `send_interactive_reply` 发送卡片

---

## 功能二：TodoWrite 卡片

### 入口
Claude 端发出 `TodoWrite` 工具调用时自动拦截，渲染成精美卡片。

### 原始数据格式

```json
{"todos": [{"content": "...", "status": "completed", "activeForm": "Creating file"}]}
```

### 卡片结构（markdown）

```
📋 Todo List

| 状态 | 待办事项 | 当前动作 |
|------|----------|----------|
| ✅ | Replace annotated_text with markdown | Fixing card rendering |
| 🔄 | Add send_edit_diff_card() to client.py | Adding send_edit_diff_card() |
| ⬜ | Write tests/test_edit_diff.py | Writing tests |

✅ 所有任务已完成！（空列表时）
```

### 状态图标约定

| status | 图标 |
|--------|------|
| `pending` | ⬜ |
| `in_progress` | 🔄 |
| `completed` | ✅ |

### 实现方式

在 `reply_formatter.py` 的 `format_tool_call` 中新增 `tool_name == "TodoWrite"` 分支：
1. 解析 tool_input JSON 中的 `todos` 数组
2. 遍历 todos，映射 status → 图标
3. 渲染 markdown 表格字符串返回
4. 由 `message_handler` 现有逻辑统一通过 `send_interactive_reply` 发送

---

## 实现文件

| 文件 | 改动 |
|------|------|
| `cc_feishu_bridge/feishu/message_handler.py` | 新增 `/git` 命令分支 |
| `cc_feishu_bridge/format/reply_formatter.py` | 新增 TodoWrite 格式化逻辑 |
| `tests/test_reply_formatter.py` | 新增 TodoWrite 格式化测试 |
| `tests/test_message_handler.py` | 新增 `/git` 命令测试（如需要） |

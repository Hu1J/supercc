# Edit 工具彩色 Diff 渲染设计

## 背景

当 Claude Code 调用 Edit 工具时，bridge 当前把原始 JSON `{"old_string": "...", "new_string": "..."}` 直接放进 backtick 发给用户，信息密度低、阅读体验差。

目标：在飞书消息中展示类似 Claude Code 终端的彩色 diff 效果——红色删除行、绿色新增行。

## 方案确定

- **消息类型**：飞书 Interactive Card
- **颜色方案**：`plain_text` 组件的 `text_color` 字段，支持 `red`、`green`、`grey` 等
- **Diff 策略**：行级 LCS（最长公共子序列）对齐，与 Claude Code 终端效果一致
- **适用工具**：Edit + Write

## 卡片结构

### Edit 工具
```
Card
├── header（标题：✏️ Edit — /path/to/file）
└── div（dark 背景 #1e1e1e，内边距）
    ├── plain_text: - 删行（text_color: red）
    ├── plain_text: + 增行（text_color: green）
    └── plain_text:   上下文（text_color: grey）
```

### Write 工具
Write 工具语义为"完整覆盖文件"，old 内容全部删除，new 内容全部新增：
```
Card
├── header（标题：✏️ Write — /path/to/file）
└── div（dark 背景）
    ├── plain_text: 文件名（蓝色，bold）
    └── plain_text: + 新增全文（绿色，逐行或分块展示）
```
Write 不需要 diff 对齐，直接按行输出新增内容即可。

## Diff 算法

行级 LCS（Longest Common Subsequence）：

1. `old_lines = old_string.splitlines()`
2. `new_lines = new_string.splitlines()`
3. 计算 LCS 矩阵，得出每行是增/删/不变
4. 输出带颜色的行序列

不引入外部 diff 库，直接在代码里实现 LCS。

## 文件结构

**新增**：`cc_feishu_bridge/format/edit_diff.py`

```python
def colorize_diff(old_string, new_string) -> list[DiffLine]:
    """Edit 工具：返回带类型的行列表，type=deletion/insertion/context"""

def colorize_write(content: str) -> list[dict]:
    """Write 工具：返回带颜色的行列表，全量绿色新增"""

def format_tool_card(tool_name, file_path, lines) -> dict:
    """构建飞书 Interactive Card JSON"""
```

**修改**：`cc_feishu_bridge/format/reply_formatter.py`
- `ReplyFormatter.format_tool_call()` 里识别 `Edit` / `Write`，返回特殊 marker

**修改**：`cc_feishu_bridge/feishu/message_handler.py`
- `stream_callback` 看到 Edit/Write 时，调用新卡片方法替代原有 backtick

## 边界条件

- `tool_name` 非 Edit/Write：保持原有 backtick 格式
- Edit：`old_string` 或 `new_string` 为空 → 降级 backtick
- Write：内容按行输出，超长分块（每块不超过飞书卡片大小限制）
- Edit diff 行数超过 50 行：截断变化区域，首尾各显示 3 行上下文
- 飞书卡片消息大小限制：超长时截断并追加 `...(已截断)` 提示

## 风险

- `green` 在某些飞书主题下偏浅，考虑用 `blue` 替代绿色
- `plain_text` 元素在 dark 背景 div 内的实际渲染效果需实测
- Write 工具按行输出时，行数过多会导致消息过长，需控制单次发送量

# Feishu 文件发送 MCP 服务设计

> **Goal:** 提供一个 MCP 工具 `FeishuSendFile`，让 Claude Code 可以通过它向飞书用户发送文件/图片。

## Architecture

参照 `memory_tools.py` 的模式：
1. 新建 `cc_feishu_bridge/claude/feishu_file_tools.py`，暴露 `FeishuSendFile` 工具
2. 在 `ClaudeIntegration.query()` 的 `mcp_servers` 里追加 `feishu_file` 入口
3. 底层复用 `FeishuClient` 已有的 `upload_image` / `send_image` / `upload_file` / `send_file`
4. chat_id 从 `SessionManager.get_active_session_by_chat_id()` 自动获取

## Tool Signature

```
FeishuSendFile(file_paths: list[str])
  -> {content: [{type: "text", text: "已发送 N 个文件..."}], is_error: bool}
```

## Data Flow

```
CC 调用 FeishuSendFile(file_paths=[...])
  → feishu_file_tools.FeishuSendFile
      → SessionManager.get_active_session_by_chat_id()  # 取 chat_id
      → 对每个文件并发：
          → 扩展名 → guess_file_type(ext)  # 判断文件类型
          → 图片类型 → upload_image() → send_image()
          → 其他类型 → upload_file() → send_file()
      → 汇总结果文本返回
```

## Error Handling

- 文件不存在：`is_error: true`，返回 "文件不存在: {path}"
- 文件超过 30MB：`is_error: true`，返回 "{filename} 超过 30MB 限制"
- 未找到活跃会话：`is_error: true`，返回 "未找到活跃飞书会话"
- Feishu API 失败：`is_error: true`，返回具体错误信息

## Files

- **Create:** `cc_feishu_bridge/claude/feishu_file_tools.py`
- **Modify:** `cc_feishu_bridge/claude/integration.py:76` — 追加 `mcp_servers` 项
- **Test:** `tests/claude/test_feishu_file_tools.py`（新建）

## Session and Config Resolution

`FeishuFileTools` 需要访问 `SessionManager` 和 `FeishuClient`。由于 MCP 工具在 SDK 初始化时创建（早于 `MessageHandler`），采用延迟初始化模式：
- 在首次调用时从 `config.resolve_config_path()` + `load_config()` 初始化 `FeishuClient`
- `SessionManager` 同理，从 `sessions.db` 路径延迟构建
- 单例模式（双重检查锁定），线程安全

## No New Dependencies

不引入新依赖，复用现有 `lark-oapi` / `claude_agent_sdk` / `sqlite3`。

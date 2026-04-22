# Claude Code 飞书桥接插件设计

## 1. 概述与目标

**项目名称:** `cc-feishu-bridge`

让用户在飞书聊天窗口中直接与本地运行的 Claude Code 对话，同时支持 Claude 操作飞书文档等企业资源。

**核心场景:** 单用户本地使用（用户自己的电脑运行 Claude Code，通过飞书远程控制）

**部署形态:** 独立的 CLI 程序（打包为单文件可执行文件，不依赖用户本地 Python 环境）。用户通过 `cc-feishu-bridge --config config.yaml` 启动，服务长期运行。

**技术栈:**
- Python 3.11+
- `claude-agent-sdk` — 连接本地 Claude Code
- `feishu-sdk` — 飞书开放平台 SDK
- SQLite — 会话存储

---

## 2. 架构

```
┌─────────────────────────────────────────────────────────────┐
│                        飞书                                  │
│   [用户消息] ──────────────────────────────────→ [Bot 接收]  │
│                     ←────────────────────────────────── [响应消息] │
└────────────────────────────┬────────────────────────────────┘
                             │ 轮询 / Webhook
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                   FeishuClient                              │
│         (feishu-sdk, 消息接收/发送/格式化)                   │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                 MessageHandler                               │
│     (安全检查、命令路由、typing 指示器)                       │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                ClaudeIntegration                            │
│      (claude-agent-sdk, 流式响应、会话管理)                   │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                  SessionManager                              │
│            (SQLite, 会话持久化、断点续聊)                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 核心组件

### 3.1 FeishuClient

- **职责:** 通过飞书开放平台 API 接收用户消息、发送响应
- **传输模式:** Webhook（生产）或轮询（开发/备用）
- **消息类型:** 文本、图片、文件、语音（转文字）、引用消息
- **输出:** 支持普通文本消息和交互式卡片（流式响应时使用）

### 3.2 MessageHandler

- **职责:**
  - 用户白名单验证（`ALLOWED_USERS`，飞书 `open_id`）
  - 输入安全检查（路径穿越、命令注入等）
  - 命令路由（`/new` 新会话、`/status` 状态等）
  - typing 指示器管理
- **会话路由:** 消息 → 查找用户活跃会话 → 继续或新建

### 3.3 ClaudeIntegration

- **职责:**
  - 调用 `claude-agent-sdk` 的 `ClaudeSDKClient`
  - 流式响应处理（实时转发给飞书用户）
  - 超时和中断处理
- **关键接口:**
  ```python
  await client.query(
      prompt=user_message,
      cwd=working_directory,
      resume=session_id,  # 续接会话
      on_stream=stream_callback,
  )
  ```
- **会话续接:** 每次对话保存 `session_id`，下次带上 `resume=session_id` 继续

### 3.4 SessionManager

- **职责:**
  - SQLite 存储会话（`session_id`, `user_id`, `created_at`, `last_used`）
  - 每个用户一个活跃会话
  - 支持 `/new` 命令强制新建会话
- **表结构:**
  ```sql
  sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    project_path TEXT,
    created_at TIMESTAMP,
    last_used TIMESTAMP,
    total_cost REAL DEFAULT 0,
    message_count INTEGER DEFAULT 0
  )
  ```

### 3.5 ReplyFormatter

- **职责:**
  - 将 Claude 的 Markdown 响应转换为飞书支持的格式
  - 处理超长消息分段（飞书每条消息最大 4096 字符）
  - 工具调用展示（格式化工具名称和简要描述）

---

## 4. 安全模型

### 4.1 认证

- **白名单模式:** 只有 `ALLOWED_USERS` 列表中的飞书 `open_id` 可以使用
- **未授权用户:** 机器人忽略其消息，不返回任何响应

### 4.2 输入安全

- 禁止路径穿越字符（`..`, `;`, `$`, `|`, `>` 等）
- 禁止访问敏感文件（`.env`, `.ssh`, `*.pem` 等）
- 工作目录限制在 `APPROVED_DIRECTORY` 内

---

## 5. 配置项

```bash
# 飞书
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_BOT_NAME=Claude

# 认证
ALLOWED_USERS=ou_xxx1,ou_xxx2   # 飞书 open_id，白名单

# Claude
CLAUDE_API_KEY=sk-ant-...        # 可选，本地已登录则不需要
CLAUDE_CLI_PATH=/usr/local/bin/claude  # Claude Code 路径
CLAUDE_MAX_TURNS=50

# 目录限制
APPROVED_DIRECTORY=/Users/you/projects  # Claude 可访问的根目录

# 会话
SESSION_DB_PATH=./data/sessions.db
```

---

## 6. 消息流

```
用户: "帮我看看这个 bug"
  ↓
飞书 Webhook → FeishuClient
  ↓
MessageHandler: 验证白名单、安全检查
  ↓
查找用户活跃 Session
  ↓
ClaudeIntegration.query(
    prompt="帮我看看这个 bug",
    cwd=project_path,
    resume=session_id,
    on_stream=stream_callback,
)
  ↓
claude-agent-sdk → 本地 Claude Code（stdio/WebSocket）
  ↓
stream_callback:
  - 工具调用 → 格式化展示在飞书
  - 文本流 → 实时更新飞书消息（流式卡片）
  ↓
ResultMessage → ReplyFormatter → 飞书消息
  ↓
更新 Session（session_id, cost, message_count）
```

---

## 7. 命令

| 命令 | 说明 |
|------|------|
| `/new` | 强制新建一个 Claude 会话 |
| `/status` | 显示当前会话状态（会话ID、消息数、token用量） |

---

## 8. MVP 范围

### 包含
- 飞书文本消息收发
- 单用户单会话（不支持多用户并发）
- Claude Code 会话续接（断点续聊）
- 流式响应（打字机效果）
- `/new` 和 `/status` 命令
- 基本安全检查

### 不包含（后续迭代）
- 图片/文件发送给 Claude（暂不支持）
- 语音消息
- `/repo` 切换工作目录
- 多用户支持
- MCP 工具扩展

---

## 9. 项目结构

```
cc-feishu-bridge/
├── src/
│   ├── main.py              # 入口
│   ├── feishu/
│   │   ├── client.py        # FeishuClient
│   │   └── handlers.py      # 消息处理
│   ├── claude/
│   │   ├── integration.py   # ClaudeIntegration
│   │   └── session.py       # SessionManager
│   ├── security/
│   │   ├── auth.py          # 白名单认证
│   │   └── validator.py     # 输入安全检查
│   └── format/
│       └── reply.py         # ReplyFormatter
├── data/                    # SQLite DB
├── config.yaml              # 配置
├── requirements.txt
└── README.md
```

---

## 10. 依赖

```
claude-agent-sdk>=0.1.0
feishu-sdk>=1.0.0
python-dateutil
sqlite3 (标准库)
```

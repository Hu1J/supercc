# Claude Code 飞书桥接插件设计 v2

## 概述

**项目名称:** `cc-feishu-bridge`

让用户在飞书聊天窗口中直接与本地运行的 Claude Code 对话。

**核心场景:** 单用户本地使用（用户自己的电脑运行 Claude Code，通过飞书远程控制）

**部署形态:** 独立 CLI 程序（打包为单文件可执行文件，不依赖用户本地 Python 环境）

**技术栈:** Python 3.11+, claude-agent-sdk, lark-oapi, aiohttp, qrcode, SQLite

---

## 架构

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                   CLI 入口 (main.py)                       │
│         检测 config.yaml → 分发到 install 或启动服务         │
└─────────────────────────────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ↓                              ↓
    ┌─────────────────┐          ┌─────────────────────────┐
    │   安装流程        │          │   Webhook 服务           │
    │ (无配置文件时)    │          │  (有配置时)               │
    └─────────────────┘          └─────────────────────────┘
```

### 安装流程（扫码引导）

```
用户运行 cc-feishu-bridge
    ↓
检测 config.yaml 不存在
    ↓
install_flow.start()
    ├─ api.init()          → 初始化飞书应用创建
    ├─ api.begin()        → 获取 verification_uri + device_code
    ├─ qr.print(url)      → 终端打印二维码
    └─ api.poll()         → 轮询直到用户扫码完成
         ↓
    拿到: client_id, client_secret, user_open_id
         ↓
    保存到 config.yaml
         ↓
    启动 webhook 服务
```

### Webhook 服务（运行时）

```
┌─────────────────────────────────────────────────────────────┐
│                        飞书                                  │
│   [用户消息] ──────────────────────────────────→ [Bot 接收]  │
│                     ←────────────────────────────────── [响应消息] │
└────────────────────────────┬────────────────────────────────┘
                             │ Webhook POST
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                 FeishuClient (aiohttp)                     │
│              接收飞书 Webhook → 解析消息                    │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                 MessageHandler                              │
│      认证 → 命令路由 → 安全检查 → Claude 查询               │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│               ClaudeIntegration (claude-agent-sdk)            │
│                    本地 Claude Code 对话                     │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│               SessionManager (SQLite)                        │
│                    会话持久化                                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 文件结构

```
cc-feishu-bridge/
├── src/
│   ├── __init__.py
│   ├── main.py                    # CLI 入口：检测配置 → 安装或启动
│   ├── config.py                  # 配置加载与验证
│   ├── feishu/
│   │   ├── __init__.py
│   │   ├── client.py             # 飞书 API 客户端
│   │   └── message_handler.py   # 消息处理路由
│   ├── claude/
│   │   ├── __init__.py
│   │   ├── integration.py        # Claude SDK 封装
│   │   └── session_manager.py   # 会话管理（SQLite）
│   ├── security/
│   │   ├── __init__.py
│   │   ├── auth.py              # 白名单认证
│   │   └── validator.py         # 输入安全检查
│   ├── format/
│   │   ├── __init__.py
│   │   └── reply_formatter.py   # 响应格式化
│   └── install/
│       ├── __init__.py
│       ├── api.py               # 飞书 App Registration API
│       ├── qr.py                # 终端二维码打印
│       └── flow.py              # 安装状态机
├── data/                        # SQLite DB 目录
├── tests/
├── config.example.yaml
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## 核心组件

### 1. CLI 入口 (main.py)

- 检测 `config.yaml` 是否存在
- 不存在 → 调用 `install_flow.run()`
- 存在 → 调用 `run_server()`

### 2. 安装流程 (install/)

**install/api.py** — 飞书 App Registration API
- `init()` — POST `/oauth/v1/app_registration`, action=init
- `begin()` — POST `/oauth/v1/app_registration`, action=begin, archetype=PersonalAgent
- `poll(device_code)` — POST `/oauth/v1/app_registration`, action=poll

返回：`{ client_id, client_secret, user_info: { open_id } }`

**install/qr.py** — 二维码打印
- `print_url(url)` — 用 `qrcode` 库在终端打印 ASCII 二维码

**install/flow.py** — 安装状态机
```python
async def run_install_flow():
    api = FeishuInstallAPI()
    await api.init()
    result = await api.begin()  # → { verification_uri, device_code }
    print_qr(result['verification_uri_complete'])
    # 轮询直到完成
    config = await api.poll(result['device_code'])
    save_config(config)  # 写入 config.yaml
```

### 3. 飞书客户端 (feishu/client.py)

- Webhook 接收（aiohttp）
- 消息解析（incoming message → IncomingMessage）
- 消息发送（send_text）

### 4. MessageHandler (feishu/message_handler.py)

- 白名单认证
- 命令路由（`/new`, `/status`）
- 输入安全检查
- 调用 Claude → 格式化响应 → 发回飞书

### 5. ClaudeIntegration (claude/integration.py)

- 调用 `claude-agent-sdk`
- 流式响应处理
- 会话续接（session_id）

### 6. SessionManager (claude/session_manager.py)

- SQLite 存储会话
- `create_session`, `get_active_session`, `update_session`, `delete_session`

### 7. ReplyFormatter (format/reply_formatter.py)

- Markdown → 飞书格式转换
- 工具调用格式化（图标 + 名称）
- 超长消息分段（4096 字符限制）

---

## 安全模型

### 认证
- 安装时自动将自己的 open_id 加入 `allowFrom`（白名单）
- 未授权用户消息被静默忽略

### 输入安全
- 路径穿越检测（`..`）
- 命令注入检测（`;`, `$`, `|`, `>`, `` ` ``）
- 敏感文件名检测（`.env`, `.ssh`, etc.）

---

## 配置项 (config.yaml)

```yaml
feishu:
  app_id: cli_xxx
  app_secret: xxx  # 或引用外部 secret
  bot_name: Claude
  domain: feishu   # feishu 或 lark

auth:
  allowed_users:
    - ou_xxx

claude:
  cli_path: claude
  max_turns: 50
  approved_directory: /Users/you/projects

storage:
  db_path: ./data/sessions.db

server:
  host: 0.0.0.0
  port: 8080
  webhook_path: /feishu/webhook
```

---

## 依赖

| 包 | 版本 | 用途 |
|----|------|------|
| `claude-agent-sdk` | >=0.2.0 | 连接本地 Claude Code |
| `lark-oapi` | >=1.0.0 | 飞书开放平台 SDK |
| `pyyaml` | >=6.0 | 配置文件解析 |
| `python-dateutil` | >=2.8.0 | 日期时间处理 |
| `aiohttp` | >=3.9.0 | Webhook HTTP 服务器 |
| `qrcode` | >=7.4 | 终端二维码打印 |

---

## MVP 范围

### 包含
- 扫码安装（自动创建飞书应用 + 绑定）
- 飞书文本消息收发
- 单用户单会话
- Claude Code 会话续接
- 流式响应（打字机效果）
- `/new` 和 `/status` 命令
- 基本安全检查

### 不包含
- 图片/文件/语音
- MCP 工具扩展
- 多用户支持
- 飞书官方 Bot 能力外的操作

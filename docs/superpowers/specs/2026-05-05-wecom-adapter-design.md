# 企业微信（WeCom）Adapter 设计方案

## 概述

在 SuperCC 中新增企业微信（WeCom）适配器，与飞书适配器平级，采用复用飞书架构的方案实现。

## 目录结构

```
supercc/
├── install/                    # 统一安装模块
│   ├── api.py              # 飞书 OAuth API + 企业微信扫码 API
│   ├── flow.py             # 飞书安装流程 + 企业微信安装流程
│   └── qr.py              # 通用二维码渲染
│
├── adapter/
│   ├── feishu/              # 飞书适配器（已存在）
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── ws_client.py
│   │   ├── message_handler.py
│   │   ├── media.py
│   │   ├── error_notifier.py
│   │   ├── token_store.py
│   │   └── format/
│   │       ├── __init__.py
│   │       ├── reply_formatter.py
│   │       ├── agent_card.py
│   │       ├── edit_diff.py
│   │       └── questionnaire_card.py
│   │
│   └── wecom/               # 企业微信适配器（新建）
│       ├── __init__.py
│       ├── client.py          # HTTP API 客户端
│       ├── ws_client.py       # WebSocket 长连接
│       ├── message_handler.py # 消息处理核心
│       ├── media.py           # 媒体下载/路径生成（复用或新建）
│       └── format/
│           ├── __init__.py
│           ├── reply_formatter.py  # Markdown 渲染、工具结果格式化
│           ├── agent_card.py       # Agent / Codex 事件卡片
│           ├── template_card.py    # 模板卡片消息（企业微信特有）
│           ├── edit_diff.py        # 编辑差异渲染
│           └── questionnaire_card.py # AskUserQuestion 问卷卡片
```

## 核心组件设计

### 0. 入站消息结构（IncomingMessage）

与飞书 `IncomingMessage` 对齐，字段映射如下：

```python
@dataclass
class WeComIncomingMessage:
    message_id: str          # msgid
    chat_id: str             # chatid
    user_open_id: str        # from.userid
    content: str             # text.content（已处理）
    message_type: str        # msgtype（text / image / file / markdown / ...）
    create_time: str = ""
    parent_id: str = ""      # 企业微信可能不支持引用消息，留空
    thread_id: str = ""
    raw_content: str = ""    # 原始 JSON 字符串
    is_group_chat: bool = False   # chattype == "group"
    chat_type: str = "single"     # "single" | "group"
    mention_bot: bool = False     # 是否被 @（从 mention 字段解析）
    mention_ids: list[str] = field(default_factory=list)
    group_name: str = ""
```

### 1. WebSocket 长连接客户端（ws_client.py）

参考官方 `@wecom/aibot-node-sdk` 实现 Python 版本。

**连接地址**：`wss://openws.work.weixin.qq.com`

**认证参数**：botId + secret（URL query params）

**入站消息示例**：
```json
{
  "msgid": "xxx",
  "aibotid": "bot_xxx",
  "chattype": "single",
  "chatid": "xxx",
  "from": { "userid": "xxx" },
  "msgtype": "text",
  "text": { "content": "用户消息" }
}
```

**出站消息**：
```json
{ "msgtype": "markdown", "markdown": { "content": "..." } }
```

### 2. HTTP API 客户端（client.py）

与飞书 `FeishuClient` 对齐，暴露能力基本一致（内部实现不同）：

| 方法 | 飞书 | 企业微信 | 说明 |
|------|------|----------|------|
| `send_text` | ✅ | ✅ | 发文本消息 |
| `send_markdown` | — | ✅ | 企业微信原生支持 markdown 消息 |
| `send_image` | ✅ | ✅ | 发图片（企业微信通过 URL 或 base64） |
| `send_file` | ✅ | ✅ | 发文件 |
| `send_text_reply` | ✅ | ✅ | 回复文本消息 |
| `send_markdown_reply` | — | ✅ | 回复 markdown |
| `send_image_reply` | ✅ | ✅ | 回复图片 |
| `send_file_reply` | ✅ | ✅ | 回复文件 |
| `send_interactive` | ✅ | ✅ | 发模板卡片（格式不同） |
| `send_interactive_reply` | ✅ | ✅ | 回复模板卡片 |
| `send_card` | ✅ | ✅ | 发独立卡片（非回复） |
| `send_edit_diff_card` | ✅ | ✅ | 发送彩色 diff 卡片 |
| `get_message` | ✅ | ✅ | 获取单条消息内容 |
| `download_media` | ✅ | ✅ | 下载媒体文件 |
| `get_chat_history` | ✅ | ⚠️ | 企业微信可能不支持，需验证 |
| `get_chat_members` | ✅ | ⚠️ | 企业微信可能不支持，需验证 |
| `get_user_name` | ✅ | ⚠️ | 企业微信可能不支持，需验证 |
| `add_typing_reaction` | ✅ | ❌ | 企业微信无 typing indicator |
| `remove_typing_reaction` | ✅ | ❌ | 企业微信无 typing indicator |
| `check_group_permissions` | ✅ | ❌ | 企业微信无需此检查 |
| `parse_incoming_message` | ✅ | ✅ | 解析入站消息为 `IncomingMessage` |

> 企业微信不支持的接口在 `WeComClient` 中保留方法名但返回空或抛异常，保持与 `MessageHandler` 的兼容性。

### 3. 消息处理器（message_handler.py）

结构与飞书 `MessageHandler` + `SessionWorker` 对齐：

**`WeComMessageHandler`**
- 持有 `WeComClient`、`Authenticator`、`SessionManager`、`ReplyFormatter`
- `handle(message)` — 消息路由（命令直接处理，普通消息进 Worker 队列）
- `_get_or_create_worker(chat_id)` — per-chat-id Worker 池管理
- `_check_group_access(message)` — 群聊权限检查（@mention、allowlist 等）
- `_get_group_config(chat_id)` — 自动注册新群配置
- `_handle_command(message)` — 处理 `/new`、`/status`、`/stop`、`/help`、`/git`、`/model`、`/codex`、`/switch`、`/restart`、`/update`、`/memory`、`/skill`
- `_safe_send` — 安全发送（markdown 判断 → 卡片/文本）
- `_preprocess_media` — 媒体下载与本地保存
- `_trigger_memory_review` — 记忆回顾（后台 Claude 查询）

**`WeComSessionWorker`**（per-chat-id）
- `queue` — 消息队列
- `_run_loop` — 主循环（idle 超时 7 天退出）
- `_process_message` — 单条消息处理：鉴权 → 媒体预处理 → 引用检测 → 查询
- `_init_options` — 初始化 Claude SDK options（resume session）
- `_run_query` — 调用 Claude SDK，stream 回调处理工具调用
- `StreamAccumulator` — 流式文本缓冲与批量发送

**与飞书的差异**：
- 企业微信无 `typing_reaction`，`_show_typing` 可省略或留空实现
- 群聊历史注入：若企业微信 API 不支持获取群历史，则仅依赖 WebSocket 实时消息
- @mention 检测：企业微信消息中可能通过 `mention` 字段携带，需适配解析逻辑

### 4. 消息渲染（format/）

| 组件 | 职责 | 与飞书差异 |
|------|------|------------|
| `reply_formatter.py` | Markdown 渲染、工具结果格式化 | 去掉飞书特有的 `img_xxx` 图片过滤、heading 降级等优化；保留工具图标、Bash/Read/TodoWrite/Memory/Cron 格式化 |
| `agent_card.py` | Agent / Codex 事件卡片 | 输出企业微信模板卡片格式（非飞书 CardKit） |
| `template_card.py` | 模板卡片消息 | 企业微信特有，替代飞书 Interactive Card |
| `edit_diff.py` | 代码差异渲染 | `_DiffMarker`、`_MemoryCardMarker` 保留；`render()` 方法输出 markdown（企业微信通用） |
| `questionnaire_card.py` | AskUserQuestion 问卷卡片 | 输出企业微信模板卡片格式 |

### 5. 媒体处理（media.py）

与飞书 `media.py` 对齐，复用或平移：
- `make_image_path`、`make_file_path`、`make_audio_path` — 本地存储路径生成
- `save_bytes` — 字节写入文件
- `sanitize_filename` — 文件名清理
- MIME → 扩展名映射表（去掉飞书专有 `file_type`，保留通用 MIME 映射）

> 可直接复用 `supercc.adapter.feishu.media` 中的通用函数，无需重写。

## Onboarding 设计

### 扫码创建机器人 API

通过分析 `@wecom/wecom-openclaw-cli` 源码，发现企业微信公开的扫码创建机器人 API：

**API 端点**：
1. `GET https://work.weixin.qq.com/ai/qc/generate?source=wecom-cli&plat={platform}`
   - 获取二维码 scode 和 auth_url
   - plat: 1=Mac, 2=Windows, 3=Linux

2. `GET https://work.weixin.qq.com/ai/qc/query_result?scode={scode}`
   - 轮询扫码结果
   - 返回 `{ status: "success", bot_info: { botid, secret } }`

**完整流程**：
1. 调用 generate API 获取 scode + auth_url
2. 用 auth_url 生成二维码展示给用户
3. 用户用企业微信扫码
4. 轮询 query_result 直到 status=success
5. 返回 botid + secret

### install 模块实现

```python
# install/api.py
async def feishu_auth_flow(): ...    # 飞书 OAuth
async def wecom_auth_flow(): ...      # 企业微信扫码

# install/flow.py
async def run_feishu_install_flow(): ...
async def run_wecom_install_flow(): ...
```

两个平台写独立函数，不通过参数判断。

## 配置结构

```python
@dataclass
class WeComChannelConfig:
    enabled: bool = False
    bot_id: str = ""
    bot_secret: str = ""
    bot_name: str = "Claude"
    groups: dict = {}          # chat_id -> WeComGroupConfigEntry
    allowed_users: list = []

@dataclass
class WeComGroupConfigEntry:
    enabled: bool = True
    allow_from: list = []      # 允许的用户 userid 列表
    require_mention: bool = True  # 群聊是否需要 @机器人
```

**config.json 示例**：
```json
{
  "channels": {
    "feishu": { ... },
    "wecom": {
      "enabled": true,
      "bot_id": "xxx",
      "bot_secret": "xxx",
      "allowed_users": ["user1", "user2"]
    }
  }
}
```

## 与核心层集成

- `message_context.py` — 注入 `platform="wecom"`
- `session_manager.py` — 使用 `platform="wecom"` 隔离
- `memory_manager.py` — 使用 `platform="wecom"` 隔离
- `main.py` — 启动时初始化企业微信 WS 连接

**main.py 启动逻辑（与飞书对齐）**：
```python
# 伪代码
if config.channels.wecom.enabled:
    wecom_ws = WeComWSClient(
        bot_id=config.channels.wecom.bot_id,
        bot_secret=config.channels.wecom.bot_secret,
        bot_name=config.channels.wecom.bot_name,
        on_message=wecom_handler.handle,
    )
    wecom_ws.start()  # 后台 asyncio 任务
```

**跨平台注意事项**：
- `MessageHandler` 中通过 `get_current_platform()` 获取平台标识，确保 session、memory 隔离正确
- 企业微信的 `user_open_id` 对应 `from.userid`，与飞书的 `open_id` 不同，但核心层不感知具体格式
- `bot_name` 用于群聊 @mention 检测（如用户 @Claude）

## 会话隔离

按 `chat_id` 隔离，与飞书一致。

## 实现步骤

1. 创建 `adapter/wecom/` 目录结构（`__init__.py`、`media.py`）
2. 实现 WebSocket 长连接客户端（`ws_client.py`）
3. 实现 HTTP API 客户端（`client.py`）
4. 实现消息处理器（`message_handler.py` + `SessionWorker` + `StreamAccumulator`）
5. 实现消息渲染模块（`format/reply_formatter.py`、`format/edit_diff.py`、`format/agent_card.py`、`format/template_card.py`、`format/questionnaire_card.py`）
6. 实现 Onboarding（合并到 install 模块：独立函数 `wecom_auth_flow`、`run_wecom_install_flow`）
7. 配置集成（`config.json` 结构、`main.py` 启动逻辑）
8. 端到端测试（私聊、群聊 @mention、工具调用渲染、媒体接收）

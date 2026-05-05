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
│   │   ├── client.py
│   │   ├── ws_client.py
│   │   ├── message_handler.py
│   │   └── format/
│   │       ├── reply_formatter.py
│   │       ├── agent_card.py
│   │       ├── edit_diff.py
│   │       └── questionnaire_card.py
│   │
│   └── wecom/               # 企业微信适配器（新建）
│       ├── client.py
│       ├── ws_client.py
│       ├── message_handler.py
│       └── format/
│           ├── reply_formatter.py  # Markdown 渲染
│           ├── template_card.py    # 模板卡片
│           └── edit_diff.py        # 编辑差异
```

## 核心组件设计

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

负责发送消息、获取媒体等 HTTP API 调用。

### 3. 消息处理器（message_handler.py）

核心逻辑：
- 解析入站消息
- @机器人 检测（与飞书一致）
- 调用核心层处理
- 格式化回复

### 4. 消息渲染（format/）

| 组件 | 职责 |
|------|------|
| reply_formatter.py | Markdown 渲染、工具结果格式化 |
| template_card.py | 模板卡片消息（企业微信特有） |
| edit_diff.py | 代码差异渲染 |

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
    groups: dict = {}
    allowed_users: list = []
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

## 会话隔离

按 `chat_id` 隔离，与飞书一致。

## 实现步骤

1. 创建 `adapter/wecom/` 目录结构
2. 实现 WebSocket 长连接客户端
3. 实现 HTTP API 客户端
4. 实现消息处理器
5. 实现消息渲染模块
6. 实现 Onboarding（合并到 install 模块）
7. 配置集成到 main.py

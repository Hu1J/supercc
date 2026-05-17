# Multi-Platform Channel Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 SuperCC 新增 5 个聊天平台：Telegram、DingTalk、QQ、WhatsApp、微信（个人），实现与现有 Feishu/WeCom 相同的 Thin-Client 架构。

**Architecture:** 每个平台一个独立目录 `channels/<platform>/`，作为独立进程通过 WebSocket 与 core 通信。共享配置结构（`config.py` + `main.py` 统一注册），各平台实现解耦，可独立开发/测试/发布。

**Tech Stack:** `python-telegram-bot`（Telegram）、`dingtalk-stream`（钉钉）、`aiohttp`（QQ/WhatsApp Bridge/微信）、Node.js Baileys bridge（WhatsApp）、`cryptography`（微信 AES CDN）

---

## 阶段 0：通用基础设施（必须先完成）

> 所有平台共用的配置结构和注册入口，在此阶段一次性完成。

### Task 0.1: Config 结构扩展

**Files:**
- Modify: `supercc/config.py` — 添加 4 个新平台配置类

- [ ] **Step 1: 添加 TelegramChannelConfig**

在 `DingTalkChannelConfig` 之后添加：

```python
@dataclass
class TelegramChannelConfig:
    """Telegram 插件配置。"""
    enabled: bool = False
    bot_token: str = ""
    bot_name: str = "Claude"
    groups: dict = field(default_factory=dict)  # group_id -> GroupConfigEntry
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class QQChannelConfig:
    """QQ 插件配置。"""
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    bot_openid: str = ""  # 机器人自己的 openid
    groups: dict = field(default_factory=dict)
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class WhatsAppChannelConfig:
    """WhatsApp 插件配置（需要 Node.js bridge）。"""
    enabled: bool = False
    bridge_port: int = 3000
    session_dir: str = ""  # 扫码 session 存储路径
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class WeChatChannelConfig:
    """微信个人（iLink）插件配置。"""
    enabled: bool = False
    token: str = ""  # 扫码登录后获得的 token
    account_id: str = ""  # 账号 ID
    allowed_users: List[str] = field(default_factory=list)
```

- [ ] **Step 2: 更新 ChannelsConfig**

```python
@dataclass
class ChannelsConfig:
    feishu: FeishuChannelConfig = field(default_factory=FeishuChannelConfig)
    dingtalk: DingTalkChannelConfig = field(default_factory=DingTalkChannelConfig)
    wecom: WeComChannelConfig = field(default_factory=WeComChannelConfig)
    telegram: TelegramChannelConfig = field(default_factory=TelegramChannelConfig)
    qq: QQChannelConfig = field(default_factory=QQChannelConfig)
    whatsapp: WhatsAppChannelConfig = field(default_factory=WhatsAppChannelConfig)
    wechat: WeChatChannelConfig = field(default_factory=WeChatChannelConfig)
```

- [ ] **Step 3: 更新 ChannelsConfig 默认值**

在 `write_config` 或 `load_config` 的 `ChannelsConfig` 构造处添加新平台默认值：

```python
telegram=TelegramChannelConfig(enabled=False),
qq=QQChannelConfig(enabled=False),
whatsapp=WhatsAppChannelConfig(enabled=False),
wechat=WeChatChannelConfig(enabled=False),
```

- [ ] **Step 4: Commit**

```bash
git add supercc/config.py
git commit -m "feat(config): 添加 Telegram/QQ/WhatsApp/微信 四个新平台的配置结构"
```

---

### Task 0.2: main.py 平台注册与分发

**Files:**
- Modify: `supercc/main.py` — 添加 4 个新平台的 run_plugin 导入和分发分支

- [ ] **Step 1: 添加导入**

在 `_run_plugin_with_restart` 函数中添加：

```python
elif name == "telegram":
    from supercc.channels.telegram.__main__ import run_plugin as _run
elif name == "qq":
    from supercc.channels.qq.__main__ import run_plugin as _run
elif name == "whatsapp":
    from supercc.channels.whatsapp.__main__ import run_plugin as _run
elif name == "wechat":
    from supercc.channels.wechat.__main__ import run_plugin as _run
```

- [ ] **Step 2: 添加插件启动逻辑**

在 `start_bridge` 中（参考 feishu/wecom 的模式）为每个新平台添加：

```python
_telegram_cfg = getattr(config.channels, "telegram", None)
if _telegram_cfg and getattr(_telegram_cfg, "enabled", False) and getattr(_telegram_cfg, "bot_token", ""):
    tasks.append(
        _run_plugin_with_restart("telegram", config, data_dir),
        name="telegram-plugin"
    )
# 同理 qq, whatsapp, wechat
```

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "feat(main): 注册 Telegram/QQ/WhatsApp/微信 四个新平台的插件启动入口"
```

---

### Task 0.3: Config Channel 交互菜单

**Files:**
- Modify: `supercc/main.py` — `_run_config_channel_interactive` 添加 4 个新平台选项

- [ ] **Step 1: 添加菜单选项**

在 `wecom` 选项之后添加：

```python
questionary.Choice("📡  Telegram", value="telegram"),
questionary.Choice("📡  QQ", value="qq"),
questionary.Choice("📡  WhatsApp", value="whatsapp"),
questionary.Choice("📡  微信", value="wechat"),
```

- [ ] **Step 2: 各自的配置分支**

每个平台需要类似 `_config_telegram_interactive()` 的函数，输入 bot_token 等凭证。

**注意：先不加具体实现，只加菜单分支框架**，各平台配置流程在 Task 1-5 中单独实现。

- [ ] **Step 3: Commit**

```bash
git add supercc/main.py
git commit -m "feat(config-ui): 添加 Telegram/QQ/WhatsApp/微信 配置菜单入口"
```

---

## 阶段 1：并行实现（5 个平台可同时进行）

> 阶段 0 完成后，5 个平台完全独立，可并行用 subagent 实现。

---

### Task 1: Telegram Channel（★☆☆☆☆ 最简单）

**Files:**
- Create: `supercc/channels/telegram/__init__.py`
- Create: `supercc/channels/telegram/__main__.py`
- Create: `supercc/channels/telegram/client.py`
- Create: `supercc/channels/telegram/core_client.py`
- Create: `supercc/channels/telegram/ws_client.py`
- Create: `supercc/channels/telegram/format/__init__.py`
- Create: `supercc/channels/telegram/format/reply_formatter.py`
- Create: `supercc/install/telegram_flow.py`
- Modify: `supercc/config.py` — TelegramChannelConfig 已完成（Task 0.1）
- Modify: `supercc/main.py` — 注册已添加（Task 0.2）

**Hermes 参考：** `~/.hermes/hermes-agent/gateway/platforms/telegram.py`

- [ ] **Step 1: 创建目录结构和 `__init__.py`**

```python
# supercc/channels/telegram/__init__.py
"""Telegram channel plugin."""
```

- [ ] **Step 2: `ws_client.py` — 继承 python-telegram-bot Application**

```python
from __future__ import annotations
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CallbackContext

logger = logging.getLogger(__name__)


class TelegramWSClient:
    def __init__(self, bot_token: str, on_message):
        self._bot_token = bot_token
        self._on_message = on_message
        self._app: Application | None = None
        self._running = False

    async def _handle_update(self, update: Update, context: CallbackContext):
        if not update.message or not update.message.text:
            return
        body = {
            "msgid": str(update.message.message_id),
            "chat_id": str(update.message.chat_id),
            "user_id": str(update.message.from_user.id),
            "username": update.message.from_user.username or "",
            "content": update.message.text,
            "date": update.message.date.isoformat() if update.message.date else "",
        }
        await self._on_message(body)

    def start(self) -> None:
        self._app = Application.builder().token(self._bot_token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_update))
        self._running = True
        self._app.run_webhook(listen="127.0.0.1", port=18792, url_path="telegram")
        # 或者用 polling: self._app.run_polling()

    def close(self) -> None:
        if self._app:
            asyncio.create_task(self._app.stop())
        self._running = False
```

- [ ] **Step 3: `core_client.py` — Thin Client 连接 Core WS**

参考 `channels/wecom/core_client.py` 模式，实现：
- `connect()` — 连接 `ws://127.0.0.1:{core_port}`
- `send_message(inbound)` — 发送 JsonRpcRequest 到 core
- `_handle_core_message(data)` — 处理 core 返回的 RESPONSE / STREAM_CHUNK / TOOL_CALL
- `_do_send_text(chat_id, text, message_id)` — 通过 `telegram_client` 发送 reply

**关键：** `TelegramClient.send_message` 使用 `reply_to_message_id` 参数实现回复关联。

- [ ] **Step 4: `client.py` — TelegramClient 封装**

```python
from telegram import Bot, ParseMode

class TelegramClient:
    def __init__(self, bot_token: str):
        self._bot = Bot(bot_token)

    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None) -> str:
        msg = await self._bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=reply_to
        )
        return str(msg.message_id)
```

**Markdown 转换规则（MARKDOWN_V2 需要转义）：**
```python
def _escape_markdown_v2(text: str) -> str:
    # 转义: _ * [ ] ( ) ~ ` > # + - = | { } .
    for ch in "_*[]()~`>#+-|{}.":
        text = text.replace(ch, f"\\{ch}")
    return text
```

- [ ] **Step 5: `format/reply_formatter.py`**

复用 `channels/common/format/` 中的组件，只需要支持 `EditDiffMarker`（Telegram 无原生卡片）。

- [ ] **Step 6: `__main__.py` — 入口**

```python
async def run_plugin(config, data_dir):
    cfg = config.channels.telegram
    ws_client = TelegramWSClient(cfg.bot_token, on_message=core_client.send_message)
    client = TelegramClient(cfg.bot_token)
    core_client = TelegramCoreWSClient(
        core_url=f"ws://127.0.0.1:{config.core.port}",
        ws_client=ws_client,
        telegram_client=client,
    )
    await core_client.connect()
    ws_client.start()
    while True:
        await asyncio.sleep(3600)
```

- [ ] **Step 7: `install/telegram_flow.py`**

简化版安装流程：用户输入 bot_token，保存到 config。

- [ ] **Step 8: Commit**

```bash
git add supercc/channels/telegram/ supercc/install/telegram_flow.py
git commit -m "feat(telegram): 添加 Telegram channel 插件实现"
```

---

### Task 2: DingTalk Channel（★★☆☆☆）

**Files:**
- Create: `supercc/channels/dingtalk/__init__.py`
- Create: `supercc/channels/dingtalk/__main__.py`
- Create: `supercc/channels/dingtalk/client.py`
- Create: `supercc/channels/dingtalk/core_client.py`
- Create: `supercc/channels/dingtalk/ws_client.py`
- Create: `supercc/channels/dingtalk/format/__init__.py`
- Create: `supercc/channels/dingtalk/format/reply_formatter.py`
- Create: `supercc/channels/dingtalk/format/ai_card.py`
- Create: `supercc/install/dingtalk_flow.py`
- Modify: `supercc/config.py` — `DingTalkChannelConfig` 已存在（Task 0.1 验证）
- Modify: `supercc/main.py` — 注册已添加（Task 0.2）

**Hermes 参考：** `~/.hermes/hermes-agent/gateway/platforms/dingtalk.py`

**注意：** `DingTalkChannelConfig` 已在 `config.py` 存在（v1.1.0 skill 发布前就有）。

- [ ] **Step 1: `ws_client.py` — 继承 dingtalk_stream.ChatbotHandler**

```python
import dingtalk_stream
from dingtalk_stream import ChatbotHandler

class DingTalkWSClient(ChatbotHandler):
    def __init__(self, app_key: str, app_secret: str, on_message):
        super().__init__()
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_message = on_message

    async def process(self, data: dict) -> dict:
        # data: {"ConversationId": "...", "SenderNick": "...", "Text": "..."}
        body = {
            "msgid": data.get("msgId") or data.get("messageId", ""),
            "chat_id": data.get("conversationId", ""),
            "user_id": data.get("senderNick", ""),
            "content": data.get("text", ""),
            "conversation_type": data.get("conversationType", ""),
        }
        await self._on_message(body)
        return {"status": "success"}
```

- [ ] **Step 2: `client.py` — 支持 send_text 和 AI Card**

```python
class DingTalkClient:
    def __init__(self, app_key: str, app_secret: str):
        self._app_key = app_key
        self._app_secret = app_secret
        self._session_webhook = ""

    async def send_text(self, chat_id: str, text: str) -> str:
        payload = {
            "msg": {
                "msgtype": "markdown",
                "markdown": {"title": "Claude", "text": text}
            }
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.post(self._session_webhook, json=payload) as resp:
                return str((await resp.json()).get("msgId", ""))
```

- [ ] **Step 3: AI Card 流式（可选但推荐）**

参考 Hermes `dingtalk.py` 的 `_create_and_stream_card()` 逻辑，先实现普通 markdown 发送，AI Card 作为后续增强。

- [ ] **Step 4: `core_client.py`**

与 Telegram 类似，实现 `send_message` + `DingTalkCoreWSClient`，注意 DingTalk 用 `reply_to_message_id` 做回复关联。

- [ ] **Step 5: Commit**

```bash
git add supercc/channels/dingtalk/ supercc/install/dingtalk_flow.py
git commit -m "feat(dingtalk): 添加 DingTalk channel 插件实现"
```

---

### Task 3: QQ Channel（★★★★☆ 最复杂之一）

**Files:**
- Create: `supercc/channels/qq/__init__.py`
- Create: `supercc/channels/qq/__main__.py`
- Create: `supercc/channels/qq/client.py`
- Create: `supercc/channels/qq/core_client.py`
- Create: `supercc/channels/qq/ws_client.py`
- Create: `supercc/channels/qq/format/__init__.py`
- Create: `supercc/channels/qq/format/reply_formatter.py`
- Create: `supercc/install/qq_flow.py`

**Hermes 参考：** `~/.hermes/hermes-agent/gateway/platforms/qqbot/adapter.py`

- [ ] **Step 1: `ws_client.py` — WebSocket Gateway 连接**

```python
# QQ WS 连接流程：
# 1. 获取 access_token: POST https://api.sgroup.qq.com/oauth2/access_token
# 2. 获取 gateway URL: GET https://api.sgroup.qq.com/gateway
# 3. WS 连接并发送 identify
```

入站消息类型：`C2C_MESSAGE_CREATE`（私聊）、`GROUP_AT_MESSAGE_CREATE`（群聊 @at）

- [ ] **Step 2: `client.py` — 发送消息**

出站用 REST API：`POST https://api.sgroup.qq.com/v2/groups/{group_openid}/messages`

- [ ] **Step 3: `core_client.py`**

实现 Thin Client 模式，消息路由根据 `conversationType` 区分私聊/群聊。

- [ ] **Step 4: Commit**

```bash
git add supercc/channels/qq/ supercc/install/qq_flow.py
git commit -m "feat(qq): 添加 QQ channel 插件实现"
```

---

### Task 4: WhatsApp Channel（★★★☆☆）

**Files:**
- Create: `supercc/channels/whatsapp/__init__.py`
- Create: `supercc/channels/whatsapp/__main__.py`
- Create: `supercc/channels/whatsapp/client.py`
- Create: `supercc/channels/whatsapp/core_client.py`
- Create: `supercc/channels/whatsapp/bridge_client.py`
- Create: `supercc/channels/whatsapp/format/__init__.py`
- Create: `supercc/channels/whatsapp/format/reply_formatter.py`
- Create: `scripts/whatsapp-bridge/bridge.js`（从 Hermes 复制）
- Create: `scripts/whatsapp-bridge/package.json`
- Create: `supercc/install/whatsapp_flow.py`

**Hermes 参考：** `~/.hermes/hermes-agent/scripts/whatsapp-bridge/bridge.js`

- [ ] **Step 1: 复制 Hermes WhatsApp bridge**

从 `~/.hermes/hermes-agent/scripts/whatsapp-bridge/` 复制 `bridge.js` 和 `package.json` 到 `scripts/whatsapp-bridge/`。

- [ ] **Step 2: `bridge_client.py` — HTTP client 到 Node bridge**

```python
class WhatsAppBridgeClient:
    def __init__(self, port: int = 3000):
        self._port = port
        self._base = f"http://127.0.0.1:{port}"

    async def get_messages(self) -> list:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{self._base}/messages") as resp:
                return await resp.json()

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        payload = {"chatId": chat_id, "message": text}
        if reply_to:
            payload["replyTo"] = reply_to
        async with aiohttp.ClientSession() as sess:
            async with sess.post(f"{self._base}/send", json=payload) as resp:
                return (await resp.json()).get("id", "")
```

- [ ] **Step 3: `__main__.py` — 启动 Node bridge 进程 + 连接 core**

- [ ] **Step 4: Commit**

```bash
git add supercc/channels/whatsapp/ scripts/whatsapp-bridge/ supercc/install/whatsapp_flow.py
git commit -m "feat(whatsapp): 添加 WhatsApp channel 插件（Node.js bridge 模式）"
```

---

### Task 5: WeChat Personal Channel（★★★★★ 最难）

**Files:**
- Create: `supercc/channels/wechat/__init__.py`
- Create: `supercc/channels/wechat/__main__.py`
- Create: `supercc/channels/wechat/client.py`
- Create: `supercc/channels/wechat/core_client.py`
- Create: `supercc/channels/wechat/lp_client.py`
- Create: `supercc/channels/wechat/crypto.py`
- Create: `supercc/channels/wechat/format/__init__.py`
- Create: `supercc/channels/wechat/format/reply_formatter.py`
- Create: `supercc/install/wechat_flow.py`

**Hermes 参考：** `~/.hermes/hermes-agent/gateway/platforms/weixin.py`

- [ ] **Step 1: `lp_client.py` — Long Polling 入站**

```python
class WeChatLongPollingClient:
    POLL_URL = "https://ilinkai.weixin.qq.com/ilink/bot/getupdates"
    SYNC_BUF_FILE = "wechat.sync.buf"

    async def poll(self) -> list:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                self.POLL_URL,
                json={"get_updates_buf": self._sync_buf},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                data = await resp.json()
                self._sync_buf = data.get("sync_buf", "")
                return data.get("messages", [])
```

- [ ] **Step 2: `crypto.py` — AES-128-ECB CDN 上传**

```python
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def aes128_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len] * pad_len)
    return encryptor.update(data) + encryptor.finalize()
```

- [ ] **Step 3: `client.py` — send_text + send_image（含 AES CDN）**

- [ ] **Step 4: `core_client.py` — Thin Client 模式**

- [ ] **Step 5: Commit**

```bash
git add supercc/channels/wechat/ supercc/install/wechat_flow.py
git commit -m "feat(wechat): 添加微信个人（iLink）channel 插件"
```

---

## 验证与测试

- [ ] **所有平台验证清单：**
  1. 插件能启动连接 core（`supercc gateway run` 无报错）
  2. 收到消息后 core 能收到（加 debug logging 验证）
  3. core 返回回复后消息能发出
  4. 群聊/私聊区分正确
  5. 配置菜单能正确保存凭证

---

## 依赖清单

| 平台 | pip 依赖 | 其他依赖 |
|------|---------|---------|
| Telegram | `python-telegram-bot[fast]` | — |
| DingTalk | `dingtalk-stream>=0.20` | — |
| QQ | `aiohttp` | — |
| WhatsApp | `aiohttp` | Node.js（bridge 进程） |
| 微信 | `aiohttp`, `cryptography` | — |

## 相关 Skills

- `new-channel-platform-guide`（v1.1.0）：各平台详细技术方案和目录结构
- `platform-adapter-pattern`：SuperCC 插件开发标准模式
- `thin-client-async-diagnostic`：Thin-Client WS 调试方法

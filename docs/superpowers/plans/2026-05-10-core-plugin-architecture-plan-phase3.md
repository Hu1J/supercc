# Phase 3：企业微信（WeCom）插件

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 SuperCC 中新建企业微信（WeCom）插件，验证「核心服务 + 平台插件」架构的可扩展性。企业微信插件遵循与飞书插件相同的 Thin Client 模式，连接到同一核心服务。

**Architecture:** WeCom 插件通过 WebSocket 连接核心服务（`adapter/wecom/ws_client.py` → `adapter/wecom/core_client.py`），与飞书插件共用 `core/executor.py` 和 `core/worker.py`。

**Tech Stack:** Python asyncio + websockets + 企业微信开放平台 API

---

## 文件清单

| 文件 | 职责 |
|------|------|
| `adapter/wecom/__init__.py` | 包入口 |
| `adapter/wecom/client.py` | WeCom HTTP API 客户端（发送消息、上传媒体等） |
| `adapter/wecom/ws_client.py` | WeCom WebSocket 长连接客户端 |
| `adapter/wecom/core_protocol.py` | 消息转换：WeCom 消息 ↔ InboundMessage |
| `adapter/wecom/core_client.py` | WeCom Thin Client（连接核心服务） |
| `adapter/wecom/format/reply_formatter.py` | Markdown 渲染（复用飞书逻辑） |

---

## Task 1: 创建 adapter/wecom/ 目录结构

**Files:**
- Create: `supercc/adapter/wecom/__init__.py`
- Create: `supercc/adapter/wecom/client.py`
- Create: `supercc/adapter/wecom/ws_client.py`
- Create: `supercc/adapter/wecom/core_protocol.py`
- Create: `supercc/adapter/wecom/core_client.py`
- Create: `supercc/adapter/wecom/format/__init__.py`
- Create: `supercc/adapter/wecom/format/reply_formatter.py`

- [ ] **Step 1.1: 创建 wecom/__init__.py**

```python
"""企业微信（WeCom）适配器 — Thin Client 模式连接核心服务。"""
from supercc.adapter.wecom.core_client import WeComCoreWSClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound
```

- [ ] **Step 1.2: 创建 wecom/client.py — WeCom API 客户端**

```python
"""WeCom HTTP API 客户端：发送消息、媒体上传等。"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


async def _call_api(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """通用 HTTP 调用。"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=body) as resp:
            return await resp.json()


@dataclass
class WeComOutgoingMessage:
    """外发消息结构。"""
    chat_id: str
    msg_type: str
    content: str  # text/markdown content or card JSON


class WeComClient:
    """WeCom HTTP API 客户端。"""

    BASE_URL = "https://qyapi.weixin.qq.com"

    def __init__(self, corp_id: str, agent_id: str, corp_secret: str):
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.corp_secret = corp_secret
        self._access_token: str | None = None

    async def _get_token(self) -> str:
        """获取 access_token。"""
        if self._access_token:
            return self._access_token
        url = f"{self.BASE_URL}/cgi-bin/gettoken"
        params = {"corpid": self.corp_id, "corpsecret": self.corp_secret}
        data = await _call_api("GET", url, {}, None)
        self._access_token = data["access_token"]
        return self._access_token

    async def send_text(self, chat_id: str, text: str) -> str:
        """发送文本消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text},
        }
        data = await _call_api("POST", url, {}, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send failed: {data}")
        return data.get("msgid", "")

    async def send_markdown(self, chat_id: str, content: str) -> str:
        """发送 Markdown 消息（企业微信原生支持）。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "markdown",
            "agentid": self.agent_id,
            "markdown": {"content": content},
        }
        data = await _call_api("POST", url, {}, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send markdown failed: {data}")
        return data.get("msgid", "")

    async def upload_media(self, file_data: bytes, file_name: str, media_type: str = "file") -> str:
        """上传临时媒体，返回 media_id。"""
        import aiohttp
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/media/upload"
        params = {"access_token": token, "type": media_type}
        form = aiohttp.FormData()
        form.add_field("media", file_data, filename=file_name, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params, data=form) as resp:
                data = await resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom upload failed: {data}")
        return data["media_id"]

    async def send_file(self, chat_id: str, media_id: str) -> str:
        """发送文件消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "file",
            "agentid": self.agent_id,
            "file": {"media_id": media_id},
        }
        data = await _call_api("POST", url, {}, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send file failed: {data}")
        return data.get("msgid", "")

    async def send_image(self, chat_id: str, media_id: str) -> str:
        """发送图片消息。"""
        token = await self._get_token()
        url = f"{self.BASE_URL}/cgi-bin/message/send"
        params = {"access_token": token}
        body = {
            "touser": chat_id,
            "msgtype": "image",
            "agentid": self.agent_id,
            "image": {"media_id": media_id},
        }
        data = await _call_api("POST", url, {}, body)
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom send image failed: {data}")
        return data.get("msgid", "")
```

- [ ] **Step 1.3: 创建 wecom/ws_client.py — WeCom WebSocket 长连接**

```python
"""WeCom WebSocket 长连接客户端（AI Bot 模式）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import hashlib
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]  # raw message dict


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 长连接客户端。

    连接地址: wss://openws.work.weixin.qq.com
    认证: botId + secret（URL query params）
    """

    WS_URL = "wss://openws.work.weixin.qq.com"

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        on_message: MessageCallback | None = None,
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._on_message = on_message
        self._ws: Any = None
        self._running = False
        self._seq = 0

    def _make_sign(self, timestamp: str) -> str:
        """生成签名。"""
        s = f"{self.bot_id}{timestamp}{self.bot_secret}"
        return hashlib.sha256(s.encode()).hexdigest()

    async def connect(self):
        """建立 WebSocket 连接。"""
        import aiohttp
        timestamp = str(int(time.time()))
        sign = self._make_sign(timestamp)
        params = f"botId={self.bot_id}&timestamp={timestamp}&sign={sign}"
        url = f"{self.WS_URL}?{params}"

        session = aiohttp.ClientSession()
        self._ws = await session.ws_connect(url, autoping=False)
        self._running = True
        logger.info("[WeComWS] Connected")
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """持续读取服务器消息。"""
        import aiohttp
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_message(data)
                except Exception:
                    logger.exception("[WeComWS] Error handling message")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[WeComWS] WS error: {self._ws.exception()}")
                break

    async def _handle_message(self, data: dict):
        """处理入站消息。"""
        msg_type = data.get("msgtype", "")
        if msg_type in ("text", "image", "file", "voice"):
            if self._on_message:
                await self._on_message(data)

    async def send(self, payload: dict) -> None:
        """发送消息到服务器。"""
        if self._ws:
            await self._ws.send_str(json.dumps(payload))

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()
```

- [ ] **Step 1.4: 创建 wecom/core_protocol.py — 消息转换**

```python
"""消息转换：WeCom 消息 ↔ 核心 InboundMessage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from core.protocol import OutboundMessage


def incoming_to_inbound(
    msg: dict,
    bot_id: str,
    project_path: str,
) -> InboundMessage:
    """
    将 WeCom 消息字典转换为核心 InboundMessage。

    WeCom 入站消息格式:
    {
        "msgid": "xxx",
        "aibotid": "bot_xxx",
        "chattype": "single" | "group",
        "chatid": "oc_xxx",
        "from": { "userid": "ou_xxx" },
        "msgtype": "text" | "image" | "file",
        "text": { "content": "..." },  # text 类型
        "image": { "media_id": "..." },  # image 类型
        ...
    }
    """
    msg_type = msg.get("msgtype", "text")
    content = ""
    if msg_type == "text":
        content = msg.get("text", {}).get("content", "")
    elif msg_type == "image":
        content = "[图片]"
    elif msg_type == "file":
        content = "[文件]"
    elif msg_type == "voice":
        content = "[语音]"

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="wecom",
        chat_id=msg.get("chatid", ""),
    )

    msg_type_map = {
        "text": MessageType.TEXT,
        "image": MessageType.IMAGE,
        "file": MessageType.FILE,
        "voice": MessageType.FILE,
    }

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=msg.get("msgid", ""),
        role=MessageRole.USER,
        content=content,
        message_type=msg_type_map.get(msg_type, MessageType.TEXT),
        media_path=None,
        user_open_id=msg.get("from", {}).get("userid", ""),
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": str(msg),
            "is_group_chat": msg.get("chattype") == "group",
            "mention_bot": bot_id in str(msg),  # 简化检测
            "mention_ids": [],
            "group_name": "",
            "chat_type": msg.get("chattype", "single"),
        },
    )


@dataclass
class WeComRenderable:
    """可渲染的企业微信消息单元。"""
    chat_id: str
    content: str  # Markdown
    use_markdown: bool = True


def outbound_to_renderable(outbound: "OutboundMessage") -> WeComRenderable:
    """将核心 OutboundMessage 转换为企业微信可渲染格式。"""
    return WeComRenderable(
        chat_id=outbound.session_key.chat_id,
        content=outbound.content,
        use_markdown=True,  # WeCom 原生支持 Markdown
    )
```

- [ ] **Step 1.5: 创建 wecom/core_client.py — WeCom Thin Client**

```python
"""企业微信插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.protocol import JsonRpcRequest, Event
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable

logger = logging.getLogger(__name__)


class WeComCoreWSClient:
    """
    企业微信插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 WeCom 消息转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为企业微信格式并发送
    """

    def __init__(
        self,
        core_url: str,
        wecom_client: WeComClient,
        bot_id: str,
        project_path: str,
    ):
        self.core_url = core_url
        self.wecom = wecom_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._id_counter = 0

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info("[WeComCore] Connected to core")
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        import websockets
        while self._running and self._ws:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)
                await self._handle_core_message(data)
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception:
                logger.exception("[WeComCore] Error")

    async def _handle_core_message(self, data: dict):
        if "id" in data:
            req_id = str(data.get("id"))
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                fut.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)

    async def _render_and_send(self, params: dict):
        renderable = outbound_to_renderable(
            type("Outbound", (), {"content": params.get("content", ""), "session_key": type("SK", (), {"chat_id": params.get("chat_id", "")})()})()
        )
        if renderable.use_markdown:
            await self.wecom.send_markdown(renderable.chat_id, renderable.content)
        else:
            await self.wecom.send_text(renderable.chat_id, renderable.content)

    async def send_message(self, msg: dict) -> dict:
        """将 WeCom 消息转发给核心。"""
        inbound = incoming_to_inbound(msg, bot_id=self.bot_id, project_path=self.project_path)

        req = JsonRpcRequest(
            id=self._next_id(),
            method="wecom.message",
            params={
                "message_id": inbound.message_id,
                "bot_id": inbound.session_key.bot_id,
                "chat_id": inbound.session_key.chat_id,
                "user_open_id": inbound.user_open_id,
                "platform": inbound.session_key.platform,
                "project_path": inbound.session_key.project_path,
                "content": inbound.content,
                "message_type": inbound.message_type.value,
                "is_group_chat": inbound.extra.get("is_group_chat", False),
                "mention_bot": inbound.extra.get("mention_bot", False),
                "mention_ids": [],
                "group_name": "",
                "extra": inbound.extra,
            },
        )

        future = asyncio.Future()
        self._pending_responses[str(req.id)] = future
        await self._ws.send(json.dumps(req.to_dict()))
        result = await future
        return result or {}

    async def _send_event(self, method: str, params: dict):
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._ws:
            await self._ws.send(json.dumps(frame))

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
```

- [ ] **Step 1.6: 创建 wecom/format/reply_formatter.py**

```python
"""企业微信消息格式化 — 复用飞书 ReplyFormatter 的核心逻辑。"""
from __future__ import annotations

# WeCom 原生支持 Markdown，直接使用飞书的格式化逻辑
from supercc.adapter.feishu.format.reply_formatter import (
    ReplyFormatter,
    should_use_card,
    split_messages,
)

__all__ = ["ReplyFormatter", "should_use_card", "split_messages"]
```

- [ ] **Step 1.7: Commit**

```bash
git add supercc/adapter/wecom/
git commit -m "feat(wecom): initial WeCom adapter skeleton (client, ws_client, core_protocol, core_client)"
```

---

## Task 2: 注册 wecom.message 路由到核心

**Files:**
- Modify: `supercc/core/server.py`

- [ ] **Step 2.1: 添加 wecom.message 路由处理**

在 `_setup_core_methods()` 中添加 `wecom.message` 路由（复用 `feishu.message` 的处理逻辑）：

```python
async def _handle_wecom_message(self, req: JsonRpcRequest) -> dict:
    """处理来自企业微信插件的消息（复用 feishu.message 逻辑）。"""
    return await self._handle_message(req)

# _setup_core_methods 中:
self.router.add("wecom.message", self._handle_wecom_message)
```

- [ ] **Step 2.2: Commit**

```bash
git add core/server.py
git commit -m "feat(core): register wecom.message routing (reuses _handle_message)"
```

---

## Task 3: 创建集成测试

**Files:**
- Create: `tests/adapter/wecom/test_core_protocol.py`
- Create: `tests/adapter/wecom/test_wecom_client.py`

- [ ] **Step 3.1: 创建 wecom/core_protocol 测试**

```python
import pytest
from supercc.adapter.wecom.core_protocol import incoming_to_inbound
from core.protocol import SessionKey, MessageRole, MessageType


class TestWeComIncomingToInbound:
    def test_basic_text_conversion(self):
        msg = {
            "msgid": "msg_123",
            "aibotid": "bot_abc",
            "chattype": "single",
            "chatid": "oc_group",
            "from": {"userid": "ou_user1"},
            "msgtype": "text",
            "text": {"content": "Hello world"},
        }
        inbound = incoming_to_inbound(msg, bot_id="cli_xxx", project_path="/test")
        assert inbound.message_id == "msg_123"
        assert inbound.content == "Hello world"
        assert inbound.role == MessageRole.USER
        assert inbound.session_key.bot_id == "cli_xxx"
        assert inbound.session_key.platform == "wecom"

    def test_session_key_four_keys(self):
        msg = {
            "msgid": "msg_456",
            "chattype": "group",
            "chatid": "oc_group2",
            "from": {"userid": "ou_user2"},
            "msgtype": "text",
            "text": {"content": "hello"},
        }
        inbound = incoming_to_inbound(msg, bot_id="bot_abc", project_path="/my/project")
        key = inbound.session_key
        assert key.bot_id == "bot_abc"
        assert key.project_path == "/my/project"
        assert key.platform == "wecom"
        assert key.chat_id == "oc_group2"

    def test_image_message(self):
        msg = {
            "msgid": "img_001",
            "chattype": "single",
            "chatid": "oc_user",
            "from": {"userid": "ou_x"},
            "msgtype": "image",
            "image": {"media_id": "media123"},
        }
        inbound = incoming_to_inbound(msg, bot_id="b", project_path="/p")
        assert inbound.message_type == MessageType.IMAGE
        assert inbound.content == "[图片]"
```

- [ ] **Step 3.2: Run tests**

```bash
/opt/homebrew/bin/pytest tests/adapter/wecom/ -v
# Expected: PASS
```

- [ ] **Step 3.3: Commit**

```bash
git add tests/adapter/wecom/
git commit -m "test(wecom): add WeCom adapter tests"
```

---

## Task 4: 将 WeCom 插件集成到 main.py

**Files:**
- Modify: `supercc/main.py`

- [ ] **Step 4.1: 添加 WeCom 插件初始化**

在 Phase 2 的 `start_bridge()` 中，在飞书插件初始化之后添加企业微信插件：

```python
# ── WeCom 插件（Phase 3）─────────────────────────────────────────────────
wecom_enabled = getattr(config.channels, "wecom", None) and getattr(config.channels.wecom, "enabled", False)

if wecom_enabled:
    from supercc.adapter.wecom.client import WeComClient
    from supercc.adapter.wecom.ws_client import WeComWSClient
    from supercc.adapter.wecom.core_client import WeComCoreWSClient

    wecom_client = WeComClient(
        corp_id=config.channels.wecom.corp_id,
        agent_id=config.channels.wecom.agent_id,
        corp_secret=config.channels.wecom.corp_secret,
    )

    wecom_core_client = WeComCoreWSClient(
        core_url="ws://127.0.0.1:8765",
        wecom_client=wecom_client,
        bot_id=config.channels.wecom.agent_id,
        project_path=config.claude.approved_directory,
    )

    def on_wecom_message(msg: dict):
        asyncio.ensure_future(wecom_core_client.send_message(msg))

    wecom_ws = WeComWSClient(
        bot_id=config.channels.wecom.agent_id,
        bot_secret=config.channels.wecom.agent_secret,
        on_message=on_wecom_message,
    )

    # 在后台连接
    async def run_wecom_client():
        await wecom_core_client.connect()

    asyncio.run(run_wecom_client())
    wecom_ws.start()
```

- [ ] **Step 4.2: Commit**

```bash
git add supercc/main.py
git commit -m "feat(main): integrate WeCom thin client (Phase 3)"
```

---

## Task 5: 添加 WeComChannelConfig 到 config.py

**Files:**
- Modify: `supercc/config.py`

- [ ] **Step 5.1: 添加 WeComChannelConfig dataclass**

```python
@dataclass
class WeComChannelConfig:
    enabled: bool = False
    corp_id: str = ""
    agent_id: str = ""
    agent_secret: str = ""
    bot_name: str = "Claude"
    groups: dict = field(default_factory=dict)
    allowed_users: list = field(default_factory=list)
```

- [ ] **Step 5.2: 将其加入 ChannelsConfig**

```python
@dataclass
class ChannelsConfig:
    feishu: FeishuChannelConfig = field(default_factory=FeishuChannelConfig)
    wecom: WeComChannelConfig = field(default_factory=WeComChannelConfig)
```

- [ ] **Step 5.3: Commit**

```bash
git add supercc/config.py
git commit -m "feat(config): add WeComChannelConfig"
```

---

## 验证清单

- [ ] WeCom adapter 文件结构正确
- [ ] `incoming_to_inbound` 正确转换四元组
- [ ] 核心服务注册了 `wecom.message` 路由
- [ ] 所有新增测试通过
- [ ] WeCom 配置可写入 config.json

---

## 关键设计决策

### 1. 复用 vs 独立

企业微信插件与飞书插件共用：
- `core/executor.py`、`core/worker.py`、`core/session.py`
- `ReplyFormatter`（直接 import 复用）

企业微信独立实现：
- WebSocket 长连接协议（企业微信特有）
- 消息解析（不同平台格式）
- 媒体处理（上传 API 不同）

### 2. 媒体处理

企业微信不支持本地上传，必须先上传到微信服务器获取 media_id，再发送。这意味着图片/文件需要额外一步上传。

### 3. Markdown 支持

企业微信原生支持 Markdown（`msgtype: "markdown"`），不需要 CardKit，渲染更简单。

---

## 相关 Skills

- `platform-adapter-pattern`：插件开发标准模式（已更新 Phase 2 内容）
- `session-isolation-gotchas`：四元组隔离验证
- `core-service-architecture`：核心服务架构

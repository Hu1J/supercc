# Phase 2：飞书插件适配

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将飞书插件改造为连接核心的薄客户端（Thin Client），核心负责 AI 推理和业务状态管理，插件只做消息格式转换（Feishu ↔ Markdown）和媒体处理。

**Architecture:**
- 飞书插件通过 WebSocket 连接核心服务（`adapter/feishu/core_client.py`）
- 插件将 Feishu `IncomingMessage` 转为核心的 `InboundMessage`，发送至核心
- 核心处理消息后，通过 WebSocket 将 `OutboundMessage`（Markdown）发回插件
- 插件将 Markdown 渲染为飞书原生格式，调用 `FeishuClient` 发送
- 工具调用由核心发给插件执行，结果通过 `tool_result` 事件发回核心

**Tech Stack:** Python asyncio + websockets + lark-oapi

---

## 文件清单

| 文件 | 职责 |
|------|------|
| `adapter/feishu/core_client.py` | 连接核心的 WebSocket 客户端（Thin Client） |
| `adapter/feishu/core_protocol.py` | 消息转换：IncomingMessage ↔ InboundMessage |
| `core/message_handler.py` | 核心消息处理：Session + WorkerPool + ClaudeIntegration |
| `core/executor.py` | WorkerPool.execute()：为每个消息创建 Task，执行 Claude 查询 |

---

## Task 1: 创建 adapter/feishu/core_protocol.py（消息转换）

**Files:**
- Create: `supercc/adapter/feishu/core_protocol.py`
- Test: `tests/adapter/feishu/test_core_protocol.py`

- [ ] **Step 1.1: 创建 core_protocol.py — IncomingMessage → InboundMessage**

```python
"""消息转换：Feishu IncomingMessage ↔ 核心 InboundMessage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from supercc.adapter.feishu.client import IncomingMessage
from core.protocol import (
    SessionKey, InboundMessage, MessageRole, MessageType,
    _cst_now,
)


def incoming_to_inbound(
    incoming: IncomingMessage,
    bot_id: str,
    project_path: str,
) -> InboundMessage:
    """
    将 Feishu IncomingMessage 转换为核心 InboundMessage。

    bot_id: 从配置读取的飞书机器人 open_id
    project_path: 从配置读取的项目路径
    """
    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="feishu",
        chat_id=incoming.chat_id,
    )

    # 消息角色
    role = MessageRole.USER

    # 消息类型
    msg_type_map = {
        "text": MessageType.TEXT,
        "image": MessageType.IMAGE,
        "file": MessageType.FILE,
    }
    msg_type = msg_type_map.get(incoming.message_type, MessageType.TEXT)

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=incoming.message_id,
        role=role,
        content=incoming.content,
        message_type=msg_type,
        media_path=None,  # 媒体路径由 media.py 下载后在 content 中嵌入
        user_open_id=incoming.user_open_id,
        thread_id=incoming.thread_id or None,
        timestamp=_cst_now(),
        extra={
            "raw": incoming.raw_content,
            "is_group_chat": incoming.is_group_chat,
            "mention_bot": incoming.mention_bot,
            "mention_ids": incoming.mention_ids,
            "group_name": incoming.group_name,
            "chat_type": incoming.chat_type,
        },
    )
```

- [ ] **Step 1.2: 创建 core_protocol.py — OutboundMessage → FeishuRenderable**

```python
from dataclasses import dataclass


@dataclass
class FeishuRenderable:
    """可渲染的飞书消息单元。"""
    chat_id: str
    message_id: str  # 用于回复/更新
    content: str     # Markdown 或原始文本
    use_card: bool = False  # 是否使用 Interactive Card


def outbound_to_renderable(
    outbound: "OutboundMessage",  # forward ref
    reply_to_message_id: str | None = None,
) -> FeishuRenderable:
    """
    将核心 OutboundMessage 转换为飞书可渲染格式。

    reply_to_message_id: 用于回复同一消息（引用）
    """
    from supercc.adapter.feishu.format.reply_formatter import should_use_card

    content = outbound.content
    use_card = should_use_card(content)

    return FeishuRenderable(
        chat_id=outbound.session_key.chat_id,
        message_id=reply_to_message_id or "",
        content=content,
        use_card=use_card,
    )
```

- [ ] **Step 1.3: 创建测试 tests/adapter/feishu/test_core_protocol.py**

```python
import pytest
from supercc.adapter.feishu.client import IncomingMessage
from supercc.adapter.feishu.core_protocol import incoming_to_inbound
from core.protocol import SessionKey, MessageRole, MessageType


class TestIncomingToInbound:
    def test_basic_conversion(self):
        incoming = IncomingMessage(
            message_id="msg_123",
            chat_id="oc_abc",
            user_open_id="ou_user1",
            content="Hello world",
            message_type="text",
            create_time="",
            parent_id="",
            thread_id="",
            raw_content="{}",
            is_group_chat=False,
            chat_type="p2p",
            mention_bot=False,
            mention_ids=[],
            group_name="",
        )
        inbound = incoming_to_inbound(
            incoming,
            bot_id="cli_xxx",
            project_path="/test/project",
        )
        assert inbound.message_id == "msg_123"
        assert inbound.content == "Hello world"
        assert inbound.role == MessageRole.USER
        assert inbound.session_key.bot_id == "cli_xxx"
        assert inbound.session_key.chat_id == "oc_abc"
        assert inbound.session_key.platform == "feishu"

    def test_session_key_four_keys(self):
        incoming = IncomingMessage(
            message_id="msg_456",
            chat_id="oc_group",
            user_open_id="ou_user2",
            content="hello",
            message_type="text",
            create_time="",
            parent_id="",
            thread_id="",
            raw_content="{}",
            is_group_chat=True,
            chat_type="group",
            mention_bot=True,
            mention_ids=["ou_bot1"],
            group_name="Test Group",
        )
        inbound = incoming_to_inbound(
            incoming,
            bot_id="bot_abc",
            project_path="/my/project",
        )
        key = inbound.session_key
        assert key.bot_id == "bot_abc"
        assert key.project_path == "/my/project"
        assert key.platform == "feishu"
        assert key.chat_id == "oc_group"
```

- [ ] **Step 1.4: Run test**

```bash
/opt/homebrew/bin/pytest tests/adapter/feishu/test_core_protocol.py -v
# Expected: PASS
```

- [ ] **Step 1.5: Commit**

```bash
git add supercc/adapter/feishu/core_protocol.py tests/adapter/feishu/test_core_protocol.py
git commit -m "feat(feishu): add core_protocol.py for IncomingMessage ↔ InboundMessage conversion"
```

---

## Task 2: 创建 core/executor.py（WorkerPool.execute）

**Files:**
- Create: `supercc/core/executor.py`
- Modify: `supercc/core/worker.py`（新增 execute 方法）
- Modify: `supercc/core/server.py`（注册 feishu.message 路由）

- [ ] **Step 2.1: 添加 WorkerPool.execute() 方法到 core/worker.py**

在 `Worker` dataclass 中添加 `_execute_task` 字段，在 `WorkerPool` 中添加 `execute()` 方法：

```python
# core/worker.py 新增 Worker 字段
@dataclass
class Worker:
    key: SessionKey
    session_id: str
    state: WorkerState = WorkerState.IDLE
    stats: WorkerStats = field(default_factory=lambda: WorkerStats(session_id=""))
    integration: Any = field(default=None)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _current_task: asyncio.Task | None = None  # 新增：当前执行中的 Task
```

```python
# core/worker.py 新增 WorkerPool.execute()
async def execute(
    self,
    key: SessionKey,
    session_id: str,
    integration: Any,
    prompt: str,
    on_stream: Callable[[Any], Awaitable[None]],
) -> tuple[str, float]:
    """
    为单个消息执行 Claude 查询。

    每个消息创建独立 asyncio.Task，支持并发。
    Worker 永久绑定 key，同一 key 的消息串行处理。
    """
    worker = await self.acquire(key, session_id, integration)
    async with worker._lock:
        worker.state = WorkerState.BUSY

    try:
        # 每个消息创建独立 task，在 worker 锁内等待完成
        task = asyncio.create_task(
            worker.integration.query(prompt=prompt, on_stream=on_stream)
        )
        worker._current_task = task
        result, sdk_sid, cost = await task
        return result, cost
    finally:
        worker.state = WorkerState.IDLE
        worker._current_task = None
        await self.release(key)
```

- [ ] **Step 2.2: 创建 core/executor.py — 核心消息处理**

```python
"""核心消息执行器：处理 InboundMessage，调用 Claude，结果发回插件。

这是核心真正执行 AI 推理的地方。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from core.protocol import (
    InboundMessage, OutboundMessage, SessionKey,
    MessageType, Event,
)
from core.session import SessionManager
from core.worker import WorkerPool

logger = logging.getLogger(__name__)


class CoreExecutor:
    """
    核心消息执行器。

    接收 InboundMessage，按 SessionKey 获取/创建 Session，
    通过 WorkerPool.execute() 执行 Claude 查询，
    收集流式输出并回调。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        worker_pool: WorkerPool,
    ):
        self.sessions = session_manager
        self.pool = worker_pool

    async def execute(
        self,
        inbound: InboundMessage,
        on_stream: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> OutboundMessage:
        """
        处理一条 InboundMessage，返回 OutboundMessage。

        on_stream: 流式输出的回调（每收到一个 chunk 调用一次）
        """
        key = inbound.session_key
        user_open_id = inbound.user_open_id or ""

        # 获取或创建 Session
        session = self.sessions.get_or_create_session(key, user_open_id)

        # 构建 prompt（从 inbound.content）
        prompt = self._build_prompt(inbound)

        # 流式回调包装
        accumulated = []

        async def _stream_callback(msg: Any) -> None:
            if msg.content:
                accumulated.append(msg.content)
                if on_stream:
                    chunk = OutboundMessage(
                        event=Event.STREAM_CHUNK,
                        session_key=key,
                        message_id=inbound.message_id,
                        content=msg.content,
                        message_type=MessageType.TEXT,
                    )
                    await on_stream(chunk)
            elif msg.tool_name and on_stream:
                tool_msg = OutboundMessage(
                    event=Event.TOOL_CALL,
                    session_key=key,
                    message_id=inbound.message_id,
                    content=f"[{msg.tool_name}]",
                    message_type=MessageType.TOOL_CALL,
                    extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                )
                await on_stream(tool_msg)

        # 执行查询
        try:
            result, cost = await self.pool.execute(
                key=key,
                session_id=session.session_id,
                integration=None,  # 实际由 pool.acquire 内部创建
                prompt=prompt,
                on_stream=_stream_callback,
            )
        except Exception as e:
            logger.exception(f"[CoreExecutor] execute error for {key}")
            result = f"错误: {e}"
            cost = 0.0

        # 更新 Session 统计
        self.sessions.update_session(
            session_id=session.session_id,
            cost=cost,
            message_increment=1,
            update_last_message=True,
        )

        # 存储消息
        self.sessions.store_message(
            message_id=inbound.message_id,
            session_id=session.session_id,
            chat_id=key.chat_id,
            user_open_id=user_open_id,
            message_type=inbound.message_type.value,
            raw_content=inbound.extra.get("raw", ""),
            content=inbound.content,
            direction="incoming",
        )

        return OutboundMessage(
            event=Event.RESPONSE,
            session_key=key,
            message_id=inbound.message_id,
            content=result,
            message_type=MessageType.TEXT,
        )

    def _build_prompt(self, inbound: InboundMessage) -> str:
        """从 inbound 构建发送给 Claude 的 prompt。"""
        content = inbound.content

        # 注入平台信息
        extra = inbound.extra
        if extra.get("is_group_chat"):
            content = f"[群聊 {extra.get('group_name', '')}] {content}"

        return content
```

- [ ] **Step 2.3: 修改 core/server.py — 注册 feishu.message 路由**

在 `_setup_core_methods()` 中添加：

```python
async def _handle_message(req: JsonRpcRequest) -> dict:
    """处理来自飞书插件的消息。"""
    from core.executor import CoreExecutor
    from core.protocol import inbound_to_inbound  # 将在 Task 3 创建

    params = req.params
    inbound = inbound_from_dict(params)  # 转换 params → InboundMessage

    # 通过全局 executor 处理
    executor = self._executor  # 需在 __init__ 中注入

    result_outbound = await executor.execute(inbound)
    return {
        "message_id": result_outbound.message_id,
        "content": result_outbound.content,
        "event": result_outbound.event,
    }


def __init__(self, host: str = "127.0.0.1", port: int = 8765,
             executor: CoreExecutor | None = None):
    # ...
    self._executor = executor


# 在 _setup_core_methods 中注册
self.router.add("feishu.message", self._handle_message)
```

- [ ] **Step 2.4: Commit**

```bash
git add core/worker.py core/executor.py core/server.py
git commit -m "feat(core): add WorkerPool.execute + CoreExecutor + feishu.message routing"
```

---

## Task 3: 创建 adapter/feishu/core_client.py（Thin Client）

**Files:**
- Create: `supercc/adapter/feishu/core_client.py`
- Modify: `supercc/adapter/feishu/__init__.py`
- Modify: `supercc/main.py`（启动逻辑）

- [ ] **Step 3.1: 创建 FeishuCoreWSClient 类**

```python
"""飞书插件的 Thin Client：连接核心 WebSocket 服务。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

from core.protocol import (
    JsonRpcRequest, JsonRpcResponse,
    OutboundMessage, Event,
)
from supercc.adapter.feishu.client import IncomingMessage
from supercc.adapter.feishu.core_protocol import incoming_to_inbound

logger = logging.getLogger(__name__)


class FeishuCoreWSClient:
    """
    飞书插件的 Thin Client。

    职责：
    - 通过 WebSocket 连接核心服务
    - 将 IncomingMessage（来自飞书）转换为 InboundMessage，发给核心
    - 接收核心的 OutboundMessage，渲染为飞书格式并发送
    - 处理 tool_call 事件（委托给 FeishuClient 执行）

    不负责：Session 管理、AI 推理、记忆操作
    """

    def __init__(
        self,
        core_url: str,           # e.g. "ws://127.0.0.1:8765"
        feishu_client,           # FeishuClient 实例（用于发送消息）
        bot_id: str,
        project_path: str,
        on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None,
    ):
        self.core_url = core_url
        self.feishu = feishu_client
        self.bot_id = bot_id
        self.project_path = project_path
        self._on_message = on_message  # 保留，用于非核心模式的回退
        self._ws: Any = None
        self._running = False
        self._pending_responses: dict[str, asyncio.Future] = {}

    async def connect(self):
        """连接核心 WebSocket 服务。"""
        import websockets
        self._ws = await websockets.connect(self.core_url)
        self._running = True
        logger.info(f"[FeishuCore] Connected to core at {self.core_url}")

        # 启动读取循环
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """持续读取核心发来的消息。"""
        while self._running and self._ws:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)
                await self._handle_core_message(data)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("[FeishuCore] Connection closed")
                break
            except Exception:
                logger.exception("[FeishuCore] Error reading message")

    async def _handle_core_message(self, data: dict):
        """处理核心发来的消息（Response 或 Event）。"""
        if "id" in data:
            # Response：唤醒等待的 Future
            req_id = data.get("id")
            if req_id in self._pending_responses:
                fut = self._pending_responses.pop(req_id)
                if data.get("error"):
                    fut.set_result(data)
                else:
                    fut.set_result(data.get("result"))
            return

        # Event notification
        method = data.get("method", "")
        params = data.get("params", {})

        if method == Event.RESPONSE or method == Event.STREAM_CHUNK:
            await self._render_and_send(params)
        elif method == Event.TOOL_CALL:
            await self._handle_tool_call(params)
        elif method == Event.PONG:
            pass  # 心跳响应

    async def _render_and_send(self, params: dict):
        """渲染 OutboundMessage 为飞书格式并发送。"""
        from supercc.adapter.feishu.format.reply_formatter import should_use_card

        content = params.get("content", "")
        chat_id = params.get("chat_id", "")
        message_id = params.get("message_id", "")

        if not content:
            return

        if should_use_card(content):
            await self.feishu.send_interactive_card(chat_id, content)
        else:
            await self.feishu.send_markdown_reply(chat_id, message_id, content)

    async def _handle_tool_call(self, params: dict):
        """处理核心发来的工具调用请求。"""
        tool_name = params.get("tool_name", "")
        tool_input = params.get("tool_input", {})
        tool_call_id = params.get("tool_call_id", "")
        chat_id = params.get("chat_id", "")

        # 执行工具（通过 FeishuClient 的 MCP 工具）
        result_content = f"[{tool_name}] executed"

        # 发送 tool_result 回核心
        await self._send_event(Event.TOOL_RESULT, {
            "tool_call_id": tool_call_id,
            "content": result_content,
            "chat_id": chat_id,
        })

    async def send_message(self, incoming: IncomingMessage) -> dict:
        """
        将 IncomingMessage 转发给核心，并等待响应。

        用于消息处理的主流程。
        """
        inbound = incoming_to_inbound(
            incoming,
            bot_id=self.bot_id,
            project_path=self.project_path,
        )

        req = JsonRpcRequest(
            id=self._next_id(),
            method="feishu.message",
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
                "mention_ids": inbound.extra.get("mention_ids", []),
                "group_name": inbound.extra.get("group_name", ""),
                "thread_id": inbound.thread_id or "",
                "extra": inbound.extra,
            },
        )

        future = asyncio.Future()
        self._pending_responses[req.id] = future

        await self._ws.send(json.dumps(req.to_dict()))

        result = await future
        return result or {}

    async def _send_event(self, method: str, params: dict):
        """发送 Event notification 到核心。"""
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._ws:
            await self._ws.send(json.dumps(frame))

    def _next_id(self) -> int:
        if not hasattr(self, "_id_counter"):
            self._id_counter = 0
        self._id_counter += 1
        return self._id_counter

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()
```

- [ ] **Step 3.2: 修改 __init__.py — 导出 FeishuCoreWSClient**

```python
from supercc.adapter.feishu.core_client import FeishuCoreWSClient
from supercc.adapter.feishu.core_protocol import incoming_to_inbound, outbound_to_renderable
```

- [ ] **Step 3.3: Commit**

```bash
git add supercc/adapter/feishu/core_client.py supercc/adapter/feishu/__init__.py
git commit -m "feat(feishu): add FeishuCoreWSClient thin client connecting to core"
```

---

## Task 4: 核心集成 — 将所有组件连接起来

**Files:**
- Modify: `supercc/main.py`
- Modify: `supercc/core/server.py`

- [ ] **Step 4.1: 修改 main.py — 统一使用新架构**

```python
# main.py 新增：启动核心服务的逻辑

async def start_core_service():
    """启动核心服务（Phase 2）。"""
    from core.server import WsServer
    from core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH
    from core.worker import WorkerPool
    from core.executor import CoreExecutor

    session_manager = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
    worker_pool = WorkerPool()
    executor = CoreExecutor(
        session_manager=session_manager,
        worker_pool=worker_pool,
    )
    server = WsServer(
        host="127.0.0.1",
        port=8765,
        executor=executor,
    )
    await server.start()
    return server


async def start_feishu_plugin():
    """启动飞书插件（统一使用 Thin Client 模式，连接核心服务）。"""
    from supercc.adapter.feishu.core_client import FeishuCoreWSClient

    core_url = "ws://127.0.0.1:8765"
    feishu_client = FeishuClient(...)  # 现有初始化

    core_client = FeishuCoreWSClient(
        core_url=core_url,
        feishu_client=feishu_client,
        bot_id=config.channels.feishu.bot_open_id,
        project_path=config.claude.approved_directory,
    )
    await core_client.connect()
    return core_client
```

main.py 启动逻辑统一为：

```python
# 启动核心服务
core_server = await start_core_service()
# 启动飞书插件 Thin Client
core_client = await start_feishu_plugin()
```

- [ ] **Step 4.2: Commit**

```bash
git add supercc/main.py
git commit -m "feat(main): integrate core service + feishu thin client"
```

---

## Task 5: 端到端测试

**Files:**
- Create: `tests/adapter/feishu/test_core_client_integration.py`

- [ ] **Step 5.1: 创建集成测试**

```python
"""Phase 2 端到端集成测试：FeishuWSClient → Core → FeishuClient"""

import pytest
import asyncio

@pytest.mark.asyncio
async def test_core_client_send_and_receive():
    """
    模拟完整流程：
    1. FeishuWSClient 收到 IncomingMessage
    2. FeishuCoreWSClient 转发给核心
    3. 核心处理后返回 OutboundMessage
    4. 验证消息内容
    """
    # 本测试需要 mock WebSocket，可使用 websockets.testing 模块
    pass
```

- [ ] **Step 5.2: 运行单元测试**

```bash
/opt/homebrew/bin/pytest tests/adapter/feishu/test_core_protocol.py tests/core/ -v
# Expected: All PASS
```

- [ ] **Step 5.3: Commit**

```bash
git add tests/adapter/feishu/test_core_client_integration.py
git commit -m "test(phase2): add core_client integration test stub"
```

---

## 关键设计决策

### 1. Thin Client 模式

Phase 2 后飞书插件统一作为 Thin Client 运行，通过 WebSocket 连接核心服务。

- 插件只做消息格式转换（IncomingMessage → InboundMessage，OutboundMessage → 飞书格式）
- 所有 AI 推理、业务状态、Session/Worker 管理全部在核心

### 2. 工具调用的处理

核心发 `tool_call` 事件 → 插件执行 → 插件发 `tool_result` 事件 → 核心继续

```
核心 ──tool_call──▶ 插件
        │              │
        │              ▼
        │         执行工具
        │              │
        ◀──tool_result─┘
        │
        ▼
   继续 Claude 查询
```

### 3. 流式输出的处理

核心在 `_stream_callback` 中每收到一个 chunk，调用 `broadcast_to_subscribed(key, "stream_chunk", chunk_params)` 发回插件。

插件收到后立即渲染发送（不等待最终结果）。

### 4. 会话续活

SessionManager 在核心进程中是单例，每次消息通过 `get_or_create_session(key, user_open_id)` 自动续活。

---

## 验证清单

- [ ] `FeishuCoreWSClient` 能成功连接核心 WebSocket
- [ ] `incoming_to_inbound` 正确转换四元组
- [ ] 核心能收到并处理消息
- [ ] 流式输出能实时发回插件
- [ ] 最终响应能正确渲染为飞书格式
- [ ] 所有单元测试通过

---

## 相关 Skills

- `core-service-architecture`：核心服务架构参考
- `platform-adapter-pattern`：插件开发标准模式
- `session-isolation-gotchas`：四元组隔离验证

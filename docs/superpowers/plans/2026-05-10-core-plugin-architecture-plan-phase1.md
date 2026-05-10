# Phase 1：核心服务基础设施

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 构建 core/ 目录，完成 WebSocket Server、Worker Pool、Session 管理的核心骨架，能够启动核心服务并接受插件连接。

**Architecture:** 核心服务通过 asyncio + websockets 实现 JSON-RPC 2.0 协议，维护 chat_id → plugin_id 路由表，按 chat_id 永久绑定 Worker。

**Tech Stack:** Python asyncio + websockets + dataclass + SQLite

---

## Step 1: 创建 core/ 目录结构和基础文件

**Files:**
- Create: `supercc/core/__init__.py`
- Create: `supercc/core/protocol.py`

- [ ] **Step 1.1: 创建 core/__init__.py**

```python
"""SuperCC Core - AI reasoning and business state management."""

__version__ = "0.3.0"
```

- [ ] **Step 1.2: 创建 core/protocol.py（协议类型定义）**

```python
"""Protocol types for core <-> plugin communication."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ConnectParams:
    platform: str
    bot_id: str
    subscriptions: list[str] = field(default_factory=list)
    token: str = ""


@dataclass
class MessageParams:
    message_id: str
    bot_id: str
    chat_id: str
    user_id: str
    platform: str
    project_path: str
    content: str  # Markdown
    is_group_chat: bool = False
    mention_ids: list[str] = field(default_factory=list)
    raw: Optional[dict] = None


@dataclass
class StreamPayload:
    message_id: str
    chat_id: str
    type: str  # "text" | "tool" | "final"
    content: str  # Markdown
    done: bool = False


@dataclass
class PushPayload:
    chat_id: str
    content: str  # Markdown


@dataclass
class HelloOk:
    features: dict  # { methods: [...], events: [...] }


# JSON-RPC Frame types
@dataclass
class RequestFrame:
    type: str = "req"
    id: str = ""
    method: str = ""
    params: Optional[dict] = None


@dataclass
class ResponseFrame:
    type: str = "res"
    id: str = ""
    ok: bool = False
    payload: Optional[dict] = None
    error: Optional[dict] = None


@dataclass
class EventFrame:
    type: str = "event"
    event: str = ""
    payload: Optional[dict] = None
```

- [ ] **Step 1.3: Commit**

```bash
git add supercc/core/__init__.py supercc/core/protocol.py
git commit -m "feat(core): create core package and protocol types"
```

---

## Step 2: 实现 Session 管理

**Files:**
- Create: `supercc/core/session.py`
- Test: `tests/core/test_session.py`

- [ ] **Step 2.1: Write failing test**

```python
# tests/core/test_session.py
import pytest
import os
import tempfile
from supercc.core.session import SessionManager

def test_session_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_sessions.db")
        sm = SessionManager(db_path)

        # Create session
        session_id = sm.create_session(
            bot_id="bot_test",
            user_id="user_1",
            chat_id="chat_1",
            platform="feishu",
            project_path="/test/project"
        )
        assert session_id is not None

        # Get session
        session = sm.get_session(session_id)
        assert session.bot_id == "bot_test"
        assert session.chat_id == "chat_1"

        # Query by chat_id
        sessions = sm.get_sessions_by_chat("chat_1", "feishu")
        assert len(sessions) == 1
        assert sessions[0].bot_id == "bot_test"

        # Update sdk_session_id
        sm.update_sdk_session_id(session_id, "sdk_xxx")
        updated = sm.get_session(session_id)
        assert updated.sdk_session_id == "sdk_xxx"

def test_session_isolation_four_keys():
    """Four-key isolation: bot_id × project_path × platform × chat_id"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_sessions.db")
        sm = SessionManager(db_path)

        # Same chat_id, different bot_id -> different session
        s1 = sm.create_session("bot_a", "user_1", "chat_1", "feishu", "/proj")
        s2 = sm.create_session("bot_b", "user_1", "chat_1", "feishu", "/proj")
        assert s1 != s2

        # Same bot_id, different chat_id -> different session
        s3 = sm.create_session("bot_a", "user_1", "chat_2", "feishu", "/proj")
        assert s3 != s1
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
pytest tests/core/test_session.py -v
# Expected: FAIL - module not found
```

- [ ] **Step 2.3: Implement SessionManager**

```python
# supercc/core/session.py
"""Session management - handles sessions.db operations."""

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional
from pathlib import Path


@dataclass
class Session:
    session_id: str
    bot_id: str
    user_id: str
    chat_id: str
    platform: str
    project_path: str
    sdk_session_id: Optional[str] = None
    last_used: float = 0.0


class SessionManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                project_path TEXT NOT NULL,
                sdk_session_id TEXT,
                last_used REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_lookup
            ON sessions(bot_id, chat_id, platform, project_path)
        """)
        conn.commit()
        conn.close()

    def create_session(self, bot_id: str, user_id: str, chat_id: str,
                       platform: str, project_path: str) -> str:
        session_id = f"session_{uuid.uuid4().hex[:16]}"
        now = __import__("time").time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO sessions (session_id, bot_id, user_id, chat_id, platform, project_path, last_used, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, bot_id, user_id, chat_id, platform, project_path, now, now))
        conn.commit()
        conn.close()
        return session_id

    def get_session(self, session_id: str) -> Optional[Session]:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        return Session(*row[:7])

    def get_sessions_by_chat(self, chat_id: str, platform: str) -> list[Session]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""
            SELECT * FROM sessions
            WHERE chat_id = ? AND platform = ?
            ORDER BY last_used DESC
        """, (chat_id, platform)).fetchall()
        conn.close()
        return [Session(*r[:7]) for r in rows]

    def update_sdk_session_id(self, session_id: str, sdk_session_id: str):
        now = __import__("time").time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            UPDATE sessions SET sdk_session_id = ?, last_used = ?
            WHERE session_id = ?
        """, (sdk_session_id, now, session_id))
        conn.commit()
        conn.close()
```

- [ ] **Step 2.4: Run test to verify it passes**

```bash
pytest tests/core/test_session.py -v
# Expected: PASS
```

- [ ] **Step 2.5: Commit**

```bash
git add tests/core/test_session.py supercc/core/session.py
git commit -m "feat(core): implement SessionManager with four-key isolation"
```

---

## Step 3: 实现 Worker Pool

**Files:**
- Create: `supercc/core/worker.py`
- Test: `tests/core/test_worker.py`

**Context:** 参考 `supercc/adapter/feishu/message_handler.py` 的 `SessionWorker` 类，将其中的 ClaudeIntegration 实例化管理逻辑迁移。

- [ ] **Step 3.1: Write failing test**

```python
# tests/core/test_worker.py
import pytest
import asyncio
from supercc.core.worker import Worker, WorkerPool

def test_worker_permanent_binding():
    """Worker is permanently bound to chat_id, not reused for other chat_ids."""
    pool = WorkerPool(max_workers=10)

    # Worker for chat_1
    w1 = pool.get_or_create_worker("chat_1", bot_id="bot_1", platform="feishu", project_path="/proj")
    assert w1.chat_id == "chat_1"

    # Same chat_id returns same worker
    w1_again = pool.get_or_create_worker("chat_1", bot_id="bot_1", platform="feishu", project_path="/proj")
    assert w1 is w1_again

    # Different chat_id returns different worker
    w2 = pool.get_or_create_worker("chat_2", bot_id="bot_1", platform="feishu", project_path="/proj")
    assert w2 is not w1
    assert w2.chat_id == "chat_2"

def test_worker_max_capacity_eviction():
    """When max reached, oldest idle worker is evicted."""
    pool = WorkerPool(max_workers=2)

    w1 = pool.get_or_create_worker("chat_1", bot_id="bot_1", platform="feishu", project_path="/p")
    w2 = pool.get_or_create_worker("chat_2", bot_id="bot_1", platform="feishu", project_path="/p")

    # At capacity, adding third evicts oldest
    w3 = pool.get_or_create_worker("chat_3", bot_id="bot_1", platform="feishu", project_path="/p")

    # w1 should be evicted (oldest), w3 is active
    assert w1 not in pool._workers.values()
    assert w3 in pool._workers.values()
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
pytest tests/core/test_worker.py -v
# Expected: FAIL
```

- [ ] **Step 3.3: Implement Worker and WorkerPool**

```python
# supercc/core/worker.py
"""Worker Pool - manages per-chat-id ClaudeIntegration instances."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from supercc.core.session import SessionManager
from supercc.claude.integration import ClaudeIntegration


@dataclass
class Worker:
    chat_id: str
    bot_id: str
    platform: str
    project_path: str
    claude: ClaudeIntegration
    sdk_session_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class WorkerPool:
    def __init__(self, max_workers: int = 50, session_db_path: str = ""):
        self.max_workers = max_workers
        self.session_db_path = session_db_path
        self._workers: dict[str, Worker] = {}
        self._lock = asyncio.Lock()
        self._session_manager: Optional[SessionManager] = None

    def _get_session_manager(self) -> SessionManager:
        if self._session_manager is None:
            self._session_manager = SessionManager(self.session_db_path)
        return self._session_manager

    async def get_or_create_worker(
        self,
        chat_id: str,
        bot_id: str,
        platform: str,
        project_path: str,
        cli_path: str = "claude",
    ) -> Worker:
        async with self._lock:
            if chat_id in self._workers:
                w = self._workers[chat_id]
                w.last_used = time.time()
                return w

            # Evict oldest if at capacity
            if len(self._workers) >= self.max_workers:
                oldest = min(
                    self._workers.values(),
                    key=lambda w: w.last_used
                )
                if oldest.task and not oldest.task.done():
                    oldest.task.cancel()
                del self._workers[oldest.chat_id]

            # Create new worker
            worker = Worker(
                chat_id=chat_id,
                bot_id=bot_id,
                platform=platform,
                project_path=project_path,
                claude=ClaudeIntegration(
                    cli_path=cli_path,
                    max_turns=50,
                    approved_directory=project_path,
                ),
            )
            self._workers[chat_id] = worker
            return worker

    def get_worker(self, chat_id: str) -> Optional[Worker]:
        return self._workers.get(chat_id)
```

- [ ] **Step 3.4: Run test to verify it passes**

```bash
pytest tests/core/test_worker.py -v
# Expected: PASS
```

- [ ] **Step 3.5: Commit**

```bash
git add tests/core/test_worker.py supercc/core/worker.py
git commit -m "feat(core): implement WorkerPool with permanent chat_id binding"
```

---

## Step 4: 实现 WebSocket Server

**Files:**
- Create: `supercc/core/server.py`
- Test: `tests/core/test_server.py`

- [ ] **Step 4.1: Write failing test**

```python
# tests/core/test_server.py
import pytest
import asyncio
import json
from supercc.core.server import CoreServer, MessageHandler

def test_route_table_basic():
    """Server maintains chat_id → plugin_id routing."""
    server = CoreServer(port=0)  # port=0 means random available port

    # Simulate plugin connection with subscriptions
    server.handle_connect(
        plugin_id="plugin_feishu_1",
        platform="feishu",
        subscriptions=["chat_1", "chat_2"]
    )

    assert server.get_plugin_for_chat("chat_1") == "plugin_feishu_1"
    assert server.get_plugin_for_chat("chat_2") == "plugin_feishu_1"
    assert server.get_plugin_for_chat("chat_3") is None

def test_plugin_disconnect_clears_routes():
    """Plugin disconnect removes all its subscriptions."""
    server = CoreServer(port=0)
    server.handle_connect("plugin_1", "feishu", ["chat_1", "chat_2"])
    server.handle_disconnect("plugin_1")

    assert server.get_plugin_for_chat("chat_1") is None
    assert server.get_plugin_for_chat("chat_2") is None
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
pytest tests/core/test_server.py -v
# Expected: FAIL
```

- [ ] **Step 4.3: Implement CoreServer**

```python
# supercc/core/server.py
"""Core WebSocket Server - JSON-RPC 2.0 protocol handling."""

import asyncio
import json
import websockets
from dataclasses import dataclass, field
from typing import Optional, Callable

from supercc.core.protocol import (
    RequestFrame, ResponseFrame, EventFrame,
    ConnectParams, MessageParams, HelloOk
)


@dataclass
class PluginInfo:
    plugin_id: str
    platform: str
    subscriptions: list[str]
    websocket: websockets.WebSocketServerProtocol


class CoreServer:
    def __init__(self, host: str = "localhost", port: int = 28888):
        self.host = host
        self.port = port
        self._plugins: dict[str, PluginInfo] = {}
        self._chat_to_plugin: dict[str, str] = {}  # chat_id -> plugin_id
        self._message_handler: Optional[Callable] = None
        self._server: Optional[websockets.WebSocketServer] = None

    def set_message_handler(self, handler: Callable):
        """Set the message handler callback (receives MessageParams, returns StreamPayload)."""
        self._message_handler = handler

    def handle_connect(self, plugin_id: str, platform: str,
                      subscriptions: list[str], ws):
        self._plugins[plugin_id] = PluginInfo(
            plugin_id=plugin_id,
            platform=platform,
            subscriptions=subscriptions,
            websocket=ws
        )
        for chat_id in subscriptions:
            self._chat_to_plugin[chat_id] = plugin_id

    def handle_disconnect(self, plugin_id: str):
        if plugin_id not in self._plugins:
            return
        plugin = self._plugins[plugin_id]
        for chat_id in plugin.subscriptions:
            self._chat_to_plugin.pop(chat_id, None)
        del self._plugins[plugin_id]

    def get_plugin_for_chat(self, chat_id: str) -> Optional[str]:
        return self._chat_to_plugin.get(chat_id)

    async def send_to_plugin(self, plugin_id: str, frame: dict):
        if plugin_id not in self._plugins:
            return
        ws = self._plugins[plugin_id].websocket
        await ws.send(json.dumps(frame))

    async def broadcast_stream(self, chat_id: str, payload: dict):
        """Send stream event to all plugins subscribed to chat_id."""
        plugin_id = self.get_plugin_for_chat(chat_id)
        if plugin_id is None:
            return
        frame = {"type": "event", "event": "stream", "payload": payload}
        await self.send_to_plugin(plugin_id, frame)

    async def handle_request(self, ws, frame: dict):
        """Handle incoming JSON-RPC request."""
        req = RequestFrame(
            type=frame.get("type"),
            id=frame.get("id", ""),
            method=frame.get("method", ""),
            params=frame.get("params")
        )

        if req.method == "connect":
            await self._handle_connect(ws, req)
        elif req.method == "message":
            await self._handle_message(ws, req)
        elif req.method == "ping":
            await ws.send(json.dumps({"type": "res", "id": req.id, "ok": True}))
        else:
            await ws.send(json.dumps({
                "type": "res", "id": req.id, "ok": False,
                "error": {"code": "METHOD_NOT_FOUND", "message": f"Unknown method: {req.method}"}
            }))

    async def _handle_connect(self, ws, req: RequestFrame):
        params = req.params or {}
        plugin_id = f"plugin_{params.get('platform', 'unknown')}_{id(ws)}"

        self.handle_connect(
            plugin_id=plugin_id,
            platform=params.get("platform", ""),
            subscriptions=params.get("subscriptions", []),
            ws=ws
        )

        response = ResponseFrame(
            type="res",
            id=req.id,
            ok=True,
            payload={
                "type": "hello-ok",
                "features": {
                    "methods": ["message", "stream", "push", "ping"],
                    "events": ["stream", "push"]
                }
            }
        )
        await ws.send(json.dumps(response.__dict__))

    async def _handle_message(self, ws, req: RequestFrame):
        params = req.params or {}
        msg_params = MessageParams(
            message_id=params.get("message_id", ""),
            bot_id=params.get("bot_id", ""),
            chat_id=params.get("chat_id", ""),
            user_id=params.get("user_id", ""),
            platform=params.get("platform", ""),
            project_path=params.get("project_path", ""),
            content=params.get("content", ""),
            is_group_chat=params.get("is_group_chat", False),
            mention_ids=params.get("mention_ids", []),
            raw=params.get("raw")
        )

        if self._message_handler:
            async for stream_payload in self._message_handler(msg_params):
                await self.broadcast_stream(msg_params.chat_id, stream_payload.__dict__)

        await ws.send(json.dumps({
            "type": "res", "id": req.id, "ok": True
        }))

    async def start(self):
        self._server = await websockets.serve(self._ws_handler, self.host, self.port)
        print(f"Core server started on {self.host}:{self.port}")

    async def _ws_handler(self, ws, path):
        await ws.send(json.dumps({"type": "event", "event": "connected"}))
        async for msg in ws:
            if msg.type == websockets.TextMessage:
                try:
                    frame = json.loads(msg.data)
                    await self.handle_request(ws, frame)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({
                        "type": "res", "id": "", "ok": False,
                        "error": {"code": "PARSE_ERROR", "message": "Invalid JSON"}
                    }))

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
```

- [ ] **Step 4.4: Run test to verify it passes**

```bash
pytest tests/core/test_server.py -v
# Expected: PASS
```

- [ ] **Step 4.5: Commit**

```bash
git add tests/core/test_server.py supercc/core/server.py
git commit -m "feat(core): implement WebSocket JSON-RPC server with routing"
```

---

## Step 5: 创建 tests/core/ 目录和初始化文件

- [ ] **Step 5.1: Create test init and run all tests**

```bash
mkdir -p tests/core
touch tests/core/__init__.py
pytest tests/core/ -v
# Expected: All PASS
```

---

## Step 6: 更新 todo 并输出进度报告

All Phase 1 tasks complete. Summary of what was built:

1. **core/protocol.py** - Protocol type definitions (dataclasses)
2. **core/session.py** - SessionManager with four-key isolation (bot_id × project_path × platform × chat_id)
3. **core/worker.py** - WorkerPool with permanent chat_id binding
4. **core/server.py** - WebSocket JSON-RPC server with routing table

**Next:** Phase 2 - Feishu plugin adaptation


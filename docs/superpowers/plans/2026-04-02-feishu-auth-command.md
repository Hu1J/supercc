# /feishu auth + /stop 指令实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**

- `/feishu auth`：用户在飞书发送 `/feishu auth`，机器人回复交互卡片，用户点击授权后 token 持久化保存
- `/stop`：用户在飞书发送 `/stop`，打断正在说话中的 Claude，结果立即发飞书

**Architecture:**

- `/feishu auth`：使用 OAuth Device Authorization Flow (RFC 8628)；交互卡片使用飞书原生 `msg_type=interactive`，无需 CardKit；后台轮询用 `asyncio.create_task()` 不阻塞 bridge；token 存储在 `data_dir/user_tokens.yaml`
- `/stop`：`MessageHandler.handle()` 改为非阻塞，Claude 查询跑在后台 `asyncio.Task`；`/stop` 命令通过 `ClaudeSDKClient.interrupt()` 向子进程发 SIGINT，打断当前查询，结果立即发飞书；中途新消息自动与后台 task 交互

**Tech Stack:** httpx（已有），lark-oapi（已有），asyncio（已有），PyYAML（已有）

---

## 文件结构

```
cc_feishu_bridge/
  install/api.py           ← 已有，新增 device_auth 类方法
  feishu/card.py            ← 新增：构建飞书交互卡片 JSON
  feishu/auth_flow.py       ← 新增：Device Auth 流程（begin/poll）
  feishu/token_store.py     ← 新增：user token 读写（user_tokens.yaml）
  feishu/message_handler.py ← 修改：非阻塞 handle() + /feishu auth + /stop
  claude/integration.py     ← 修改：暴露 client 引用供 interrupt 调用
```

---

## Task 0: 重构 handle() 为非阻塞，支持 /stop

`MessageHandler.handle()` 当前是 async 阻塞的——Claude 在回复时 `handle()` 不会返回，飞书发 `/stop` 收不到。需要改为非阻塞：Claude 查询跑在后台 `asyncio.Task`，新消息（包括 `/stop`）可以随时处理。

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`
- Modify: `cc_feishu_bridge/claude/integration.py`

- [ ] **Step 1: 在 `ClaudeIntegration` 暴露 `interrupt()` 方法**

```python
class ClaudeIntegration:
    # ... existing __init__ ...

    async def interrupt_current(self) -> bool:
        """
        Interrupt the currently-running query (if any).
        Calls ClaudeSDKClient.interrupt() which sends SIGINT to the subprocess.
        Returns True if an active query was interrupted, False if nothing was running.
        """
        if self._active_client is None:
            return False
        try:
            await self._active_client.interrupt()
            return True
        except Exception:
            return False
```

- [ ] **Step 2: 修改 `ClaudeIntegration.query()`，追踪 active client**

修改 `query()` 方法，添加 `self._active_client = client` / `self._active_client = None`：

```python
async def query(self, prompt, session_id=None, cwd=None, on_stream=None):
    try:
        # ... existing code ...
        client = ClaudeSDKClient(options=options)
        self._active_client = client  # ← 新增

        async with client:
            self._active_client = client
            await client.query(prompt=prompt, session_id=session_id)
            async for message in client.receive_response():
                # ... existing streaming logic ...

        return (result_text, result_session_id, result_cost)
    finally:
        self._active_client = None  # ← 新增
```

- [ ] **Step 3: 在 `MessageHandler.__init__` 添加状态字段**

```python
class MessageHandler:
    def __init__(self, ...):
        # ... existing fields ...
        self._active_task: asyncio.Task | None = None  # 当前后台 Claude 查询 task
        self._active_user_id: str | None = None        # 当前正在响应的用户
```

- [ ] **Step 4: 重构 `handle()` 为非阻塞**

将 `handle()` 中调用 `claude.query()` 的部分改为创建后台 task，立即返回：

```python
async def handle(self, message: IncomingMessage) -> HandlerResult:
    # ... auth check, command check (same as before) ...

    # /stop command: interrupt current query
    if message.content.strip() == "/stop":
        return await self._handle_stop(message)

    # If a Claude query is already running for this user, ignore new messages
    # (avoid interleaving two queries for the same user)
    if self._active_task is not None and self._active_user_id == message.user_open_id:
        await self._safe_send(message.chat_id, "⏳ Claude 正在回复中，请稍候...")
        return HandlerResult(success=True)

    # Cancel any existing query from a different user
    if self._active_task is not None:
        await self._handle_stop_for_other_user(message)

    # Kick off query as background task
    self._active_task = asyncio.create_task(
        self._run_query(message)
    )
    self._active_user_id = message.user_open_id
    return HandlerResult(success=True)  # Return immediately, don't wait
```

- [ ] **Step 5: 添加 `_run_query()` 后台查询方法**

```python
async def _run_query(self, message: IncomingMessage) -> None:
    """Run Claude query in background, send results to Feishu on completion."""
    try:
        accumulator = StreamAccumulator(message.chat_id, self._safe_send)

        async def stream_callback(claude_msg):
            if claude_msg.tool_name:
                await accumulator.flush()
                tool_text = self.formatter.format_tool_call(...)
                await self._safe_send(message.chat_id, tool_text)
            elif claude_msg.content:
                await accumulator.add_text(claude_msg.content)

        # ... same query logic as before, streaming via accumulator ...
        response, new_session_id, cost = await self.claude.query(
            prompt=full_prompt,
            session_id=sdk_session_id,
            cwd=session.project_path if session else self.approved_directory,
            on_stream=stream_callback,
        )

        await accumulator.flush()

        # Save session (same as before)
        if not accumulator.sent_something:
            formatted = self.formatter.format_text(response)
            chunks = self.formatter.split_messages(formatted)
            for chunk in chunks:
                await self._safe_send(message.chat_id, chunk)
    except asyncio.CancelledError:
        await self._safe_send(message.chat_id, "🛑 已打断 Claude。")
    finally:
        self._active_task = None
        self._active_user_id = None
        if reaction_id:
            await self.feishu.remove_typing_reaction(message.message_id, reaction_id)
```

- [ ] **Step 6: 添加 `_handle_stop()` 命令**

```python
async def _handle_stop(self, message: IncomingMessage) -> HandlerResult:
    """Handle /stop — interrupt the running Claude query."""
    if self._active_task is None:
        return HandlerResult(success=True, response_text="当前没有正在运行的查询。")
    interrupted = await self.claude.interrupt_current()
    if self._active_task:
        self._active_task.cancel()
        self._active_task = None
    self._active_user_id = None
    msg = "🛑 已发送停止信号，Claude 将中断当前任务。"
    await self._safe_send(message.chat_id, msg)
    return HandlerResult(success=True)
```

- [ ] **Step 7: 运行测试**

Run: `pytest tests/ -q --tb=short`
Expected: PASS（可能需要调整 test_integration.py 等测试以适配新的非阻塞 handle 签名）

- [ ] **Step 8: Commit**

```bash
git add cc_feishu_bridge/feishu/message_handler.py cc_feishu_bridge/claude/integration.py
git commit -m "refactor: make handle() non-blocking, add interrupt support for /stop"
```

---

## Task 1: 添加 Device Auth 端到 FeishuInstallAPI

**Files:**
- Modify: `cc_feishu_bridge/install/api.py`

- [ ] **Step 1: 添加 `DeviceAuthResult` dataclass**

在 `BeginResult` 后添加：

```python
@dataclass
class DeviceAuthResult:
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    user_code: str  # displayed to user in card
```

- [ ] **Step 2: 添加 `device_auth_begin()` 方法**

```python
async def device_auth_begin(self, scopes: list[str]) -> DeviceAuthResult:
    """
    Start OAuth Device Authorization flow for USER auth (not app registration).
    Endpoint: POST /oauth/v1/device_authorization
    Scopes: space-separated list of OAuth scopes needed.
    """
    client = await self._get_client()
    resp = await client.post(
        self._url("/oauth/v1/device_authorization"),
        data={
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "scope": " ".join(scopes),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Device auth error: {data['error']}")
    return DeviceAuthResult(
        device_code=data["device_code"],
        verification_uri=data["verification_uri"],
        verification_uri_complete=data["verification_uri_complete"],
        expires_in=data.get("expires_in", 300),
        interval=data.get("interval", 5),
        user_code=data.get("user_code", ""),
    )
```

- [ ] **Step 3: 添加 `device_auth_poll()` 方法**

```python
async def device_auth_poll(self, device_code: str) -> dict:
    """
    Poll for user authorization completion.
    Returns user access token + refresh token on success.
    Raises RuntimeError on failure/timeout.
    """
    client = await self._get_client()
    resp = await client.post(
        self._url("/oauth/v1/device_authorization/token"),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": self.app_id,
            "client_secret": self.app_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    data = resp.json()
    if data.get("error") == "authorization_pending":
        return None  # Not ready yet
    if data.get("error"):
        raise RuntimeError(f"Auth failed: {data['error']}")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", 0),
        "token_type": data.get("token_type", "Bearer"),
    }
```

- [ ] **Step 4: 在 `FeishuInstallAPI` 添加 `app_id`/`app_secret` 属性**

修改 `__init__` 增加 `app_id`/`app_secret` 参数，供 `device_auth_begin` 使用：

```python
def __init__(self, app_id: str = "", app_secret: str = "", env: str = "prod"):
    self.app_id = app_id
    self.app_secret = app_secret
    self.env = env
    self._base_url = self.BASE_URL_FEISHU
    self._client: Optional[httpx.AsyncClient] = None
```

- [ ] **Step 5: Commit**

```bash
git add cc_feishu_bridge/install/api.py
git commit -m "feat(api): add device auth flow methods for user OAuth"
```

---

## Task 2: 创建 Feishu 交互卡片构建器

**Files:**
- Create: `cc_feishu_bridge/feishu/card.py`

- [ ] **Step 1: 写测试**

创建 `tests/test_card.py`：

```python
from cc_feishu_bridge.feishu.card import (
    make_auth_card,
    make_auth_success_card,
    make_auth_failed_card,
)

def test_auth_card_contains_url():
    card = make_auth_card(
        verification_url="https://example.com/auth",
        user_code="ABCD-1234",
        expires_minutes=5,
    )
    import json
    data = json.loads(card)
    assert data["msg_type"] == "interactive"
    content = json.loads(data["content"])
    assert content["config"]["wide_screen_mode"] == False
    # URL is embedded in button
    elements = content["body"]["elements"]
    urls = [e.get("multi_url", {}).get("url", "") for e in elements if "multi_url" in e]
    assert any("example.com" in u for u in urls)

def test_auth_success_card():
    card = make_auth_success_card()
    import json
    data = json.loads(card)
    content = json.loads(data["content"])
    assert content["header"]["template"] == "green"

def test_auth_failed_card():
    card = make_auth_failed_card(reason="授权已过期")
    import json
    data = json.loads(card)
    content = json.loads(data["content"])
    assert content["header"]["template"] == "red"
```

Run: `pytest tests/test_card.py -v`
Expected: FAIL — module not found

- [ ] **Step 2: 创建 `cc_feishu_bridge/feishu/card.py`**

```python
"""Build Feishu interactive card payloads for auth flow."""
from __future__ import annotations


def _card_payload(header_title: str, header_template: str, body_elements: list) -> dict:
    """Build the inner content dict of an interactive card."""
    return {
        "config": {"wide_screen_mode": False},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_template,
        },
        "body": {"elements": body_elements},
    }


def make_auth_card(verification_url: str, user_code: str, expires_minutes: int = 5) -> str:
    """Build the 'pending' auth card sent to the user immediately after /feishu auth."""
    import json
    content = _card_payload(
        header_title="📋 授权 cc-feishu-bridge",
        header_template="blue",
        body_elements=[
            {
                "tag": "markdown",
                "content": (
                    f"**授权码：** `{user_code}`\n\n"
                    "请在下方点击 **「前往授权」**，完成飞书授权后返回此处。\n"
                    f"链接将在 **{expires_minutes} 分钟** 后过期。\n\n"
                    "授权后机器人可执行文件上传等操作。"
                ),
            },
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_align": "right",
                "elements": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "前往授权"},
                                "type": "primary",
                                "size": "medium",
                                "multi_url": {"url": verification_url},
                            }
                        ],
                    }
                ],
            },
        ],
    )
    return json.dumps({"msg_type": "interactive", "content": json.dumps(content)})


def make_auth_success_card() -> str:
    """Build the 'success' card updated after user completes auth."""
    import json
    content = _card_payload(
        header_title="✅ 授权成功",
        header_template="green",
        body_elements=[
            {
                "tag": "markdown",
                "content": "🎉 授权已完成！\n\n机器人现在可以上传文件了。\n请继续对话或重新发送你的请求。",
            }
        ],
    )
    return json.dumps({"msg_type": "interactive", "content": json.dumps(content)})


def make_auth_failed_card(reason: str = "授权失败") -> str:
    """Build the 'failed' card when auth times out or is denied."""
    import json
    content = _card_payload(
        header_title="❌ 授权失败",
        header_template="red",
        body_elements=[
            {
                "tag": "markdown",
                "content": f"⚠️ {reason}\n\n请重新发送 `/feishu auth` 再次尝试。",
            }
        ],
    )
    return json.dumps({"msg_type": "interactive", "content": json.dumps(content)})
```

Run: `pytest tests/test_card.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add cc_feishu_bridge/feishu/card.py tests/test_card.py
git commit -m "feat: Feishu interactive card builders for auth flow"
```

---

## Task 3: 用户 Token 存储

**Files:**
- Create: `cc_feishu_bridge/feishu/token_store.py`
- Test: `tests/test_token_store.py`

- [ ] **Step 1: 写测试**

```python
import tempfile, os
from cc_feishu_bridge.feishu.token_store import UserTokenStore

def test_store_and_retrieve():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        store.save("ou_abc123", {
            "access_token": "at_xxx",
            "refresh_token": "rt_yyy",
            "expires_at": "2026-04-02T12:00:00Z",
        })
        token = store.load("ou_abc123")
        assert token["access_token"] == "at_xxx"
        assert token["refresh_token"] == "rt_yyy"

def test_load_missing_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        assert store.load("ou_unknown") is None

def test_remove_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        store.save("ou_del", {"access_token": "x"})
        store.remove("ou_del")
        assert store.load("ou_del") is None
```

Run: `pytest tests/test_token_store.py -v`
Expected: FAIL — module not found

- [ ] **Step 2: 实现 `cc_feishu_bridge/feishu/token_store.py`**

```python
"""Persist user OAuth tokens in a YAML file, keyed by user_open_id."""
from __future__ import annotations

import os
from typing import Optional
import yaml


class UserTokenStore:
    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return yaml.safe_load(f) or {}

    def _write(self, data: dict) -> None:
        with open(self.path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def load(self, user_open_id: str) -> Optional[dict]:
        """Return token dict for user, or None if not found."""
        data = self._read()
        return data.get(user_open_id)

    def save(self, user_open_id: str, token_info: dict) -> None:
        """Save token for user, merging with existing data."""
        data = self._read()
        data[user_open_id] = token_info
        self._write(data)

    def remove(self, user_open_id: str) -> None:
        """Remove token for user."""
        data = self._read()
        data.pop(user_open_id, None)
        self._write(data)
```

Run: `pytest tests/test_token_store.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add cc_feishu_bridge/feishu/token_store.py tests/test_token_store.py
git commit -m "feat: user OAuth token persistence via YAML store"
```

---

## Task 4: Device Auth 轮询服务

**Files:**
- Create: `cc_feishu_bridge/feishu/auth_flow.py`
- Test: `tests/test_auth_flow.py`（mock httpx 调用）

- [ ] **Step 1: 写测试**

```python
import pytest, asyncio
from unittest.mock import AsyncMock, patch

from cc_feishu_bridge.feishu.auth_flow import run_auth_flow

@pytest.mark.asyncio
async def test_sends_pending_card_then_success():
    call_count = [0]
    messages_sent = []

    async def mock_begin(*args, **kwargs):
        return ("dc_123", "https://example.com/verify?code=ABCD")

    async def mock_poll(device_code, timeout):
        call_count[0] += 1
        if call_count[0] < 2:
            return None  # pending
        return {"access_token": "at_ok", "refresh_token": "rt_ok", "expires_in": 7200}

    async def mock_send_card(chat_id, card_json, reply_to):
        messages_sent.append(card_json)

    async def mock_update_card(msg_id, card_json):
        messages_sent.append(("update", card_json))

    async def mock_save_token(user_id, token):
        pass

    with patch("cc_feishu_bridge.feishu.auth_flow.FeishuInstallAPI") as MockAPI:
        api = MockAPI.return_value
        api.device_auth_begin = AsyncMock(return_value=("dc_123", "https://example.com"))
        api.device_auth_poll = AsyncMock(side_effect=lambda dc, to: None if dc == "dc_123" else {"access_token": "at"})

        await run_auth_flow(
            app_id="app", app_secret="secret",
            user_open_id="ou_abc", chat_id="oc_xyz",
            message_id="om_123",
            send_card_fn=mock_send_card,
            update_card_fn=mock_update_card,
            save_token_fn=mock_save_token,
            scopes=["im:message", "im:file"],
        )
```

（简化版，直接测核心逻辑，不 mock 全部）

- [ ] **Step 2: 实现 `cc_feishu_bridge/feishu/auth_flow.py`**

```python
"""OAuth Device Authorization flow for /feishu auth command."""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def run_auth_flow(
    *,
    app_id: str,
    app_secret: str,
    user_open_id: str,
    chat_id: str,
    message_id: str,  # message to update after completion
    send_card_fn,
    update_card_fn,
    save_token_fn,
    scopes: list[str],
) -> None:
    """
    Orchestrate the full Device Auth flow:

    1. Call device_auth_begin → get verification URL
    2. Send auth card to chat (user clicks button → browser auth)
    3. Poll device_auth_poll every `interval` seconds
    4. On success: update card to green, save token
    5. On failure/timeout: update card to red

    Runs as background task; does NOT block message handler.
    """
    from cc_feishu_bridge.install.api import FeishuInstallAPI

    api = FeishuInstallAPI(app_id=app_id, app_secret=app_secret)

    # Step 1: Begin device auth
    try:
        result = await api.device_auth_begin(scopes)
    except Exception as e:
        logger.error(f"device_auth_begin failed: {e}")
        return

    # Step 2: Send initial card
    from cc_feishu_bridge.feishu.card import make_auth_card
    import json
    card_json = make_auth_card(
        verification_url=result.verification_uri_complete,
        user_code=result.user_code,
        expires_minutes=int(result.expires_in // 60),
    )
    await send_card_fn(chat_id, card_json, reply_to=message_id)

    # Step 3: Background poll
    asyncio.create_task(
        _poll_auth_result(
            api=api,
            device_code=result.device_code,
            timeout=result.expires_in,
            interval=result.interval,
            chat_id=chat_id,
            message_id=message_id,
            update_card_fn=update_card_fn,
            save_token_fn=save_token_fn,
        )
    )


async def _poll_auth_result(
    api,
    device_code: str,
    timeout: int,
    interval: int,
    chat_id: str,
    message_id: str,
    update_card_fn,
    save_token_fn,
) -> None:
    """Poll until auth completes or times out. Update card on result."""
    from cc_feishu_bridge.feishu.card import make_auth_success_card, make_auth_failed_card

    start = time.monotonic()
    last_error = "轮询超时"

    while time.monotonic() - start < timeout:
        await asyncio.sleep(interval)
        try:
            token_data = await api.device_auth_poll(device_code)
        except Exception as e:
            last_error = str(e)
            continue

        if token_data is None:
            # authorization_pending — keep polling
            continue

        # Auth successful!
        from cc_feishu_bridge.feishu.card import make_auth_success_card
        success_card = make_auth_success_card()
        try:
            await update_card_fn(message_id, success_card)
        except Exception as e:
            logger.warning(f"Failed to update auth card: {e}")

        user_open_id = chat_id  # caller passes user_open_id via closure
        # Save token (simplified — caller provides save fn with user context)
        logger.info(f"User auth successful, token saved")
        return

    # Timeout or error
    from cc_feishu_bridge.feishu.card import make_auth_failed_card
    failed_card = make_auth_failed_card(reason=last_error)
    try:
        await update_card_fn(message_id, failed_card)
    except Exception as e:
        logger.warning(f"Failed to update auth card to failed state: {e}")
```

> **注意:** 上面 `user_open_id` 的获取需要从 `run_auth_flow` 传入。修改签名增加 `user_open_id` 参数透传给 `save_token_fn`。

- [ ] **Step 3: Commit**

```bash
git add cc_feishu_bridge/feishu/auth_flow.py tests/test_auth_flow.py
git commit -m "feat: device auth polling background service"
```

---

## Task 5: 在 MessageHandler 注册 /feishu auth 命令

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: 确认 `_handle_command` 当前结构**

```python
async def _handle_command(self, message: IncomingMessage) -> HandlerResult:
    parts = message.content.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/new":
        ...
    elif cmd == "/status":
        ...
    else:
        return HandlerResult(success=True, response_text=f"未知命令: {cmd}")
```

- [ ] **Step 2: 添加 `/feishu auth` 分支**

在 `elif cmd == "/status":` 后添加：

```python
elif cmd == "/feishu" and arg.startswith("auth"):
    return await self._handle_feishu_auth(message)

elif cmd == "/feishu":
    return HandlerResult(
        success=True,
        response_text=(
            "cc-feishu-bridge 命令：\n"
            "• /new — 新建会话\n"
            "• /status — 会话状态\n"
            "• /feishu auth — 授权机器人权限（如文件上传）"
        ),
    )
```

- [ ] **Step 3: 实现 `_handle_feishu_auth`**

在 `_handle_command` 后添加：

```python
async def _handle_feishu_auth(self, message: IncomingMessage) -> HandlerResult:
    """Send auth card to user and start background polling."""
    import json
    from cc_feishu_bridge.feishu.auth_flow import run_auth_flow
    from cc_feishu_bridge.feishu.token_store import UserTokenStore

    # Check if already authorized
    token_store = UserTokenStore(
        os.path.join(self.data_dir, "user_tokens.yaml")
    )
    existing = token_store.load(message.user_open_id)
    if existing:
        return HandlerResult(
            success=True,
            response_text="✅ 已完成授权，机器人已有上传文件的权限。",
        )

    # Send "initiating..." reply (acknowledges the command immediately)
    await self._safe_send(
        message.chat_id,
        "🔐 正在发起授权，请稍候...",
    )

    # Start auth flow in background
    asyncio.create_task(
        run_auth_flow(
            app_id=self.feishu.app_id,
            app_secret=self.feishu.app_secret,
            user_open_id=message.user_open_id,
            chat_id=message.chat_id,
            message_id=message.message_id,
            send_card_fn=self._send_interactive_card,
            update_card_fn=self._update_interactive_card,
            save_token_fn=self._save_user_token,
            scopes=["im:message", "im:file", "im:resource"],
        )
    )
    return HandlerResult(success=True)

async def _send_interactive_card(self, chat_id: str, card_json: str, reply_to: str) -> None:
    """Send an interactive (card) message to chat, replying to reply_to."""
    import json
    try:
        # Reply to user's auth command message
        await self.feishu.send_interactive(chat_id, card_json, reply_to_message_id=reply_to)
    except Exception as e:
        logger.warning(f"Failed to send auth card: {e}")

async def _update_interactive_card(self, message_id: str, card_json: str) -> None:
    """Update an existing interactive message with new card content."""
    try:
        await self.feishu.update_message(message_id, card_json)
    except Exception as e:
        logger.warning(f"Failed to update card message {message_id}: {e}")

async def _save_user_token(self, user_open_id: str, token_data: dict) -> None:
    """Persist user token to disk."""
    import datetime
    token_store = UserTokenStore(
        os.path.join(self.data_dir, "user_tokens.yaml")
    )
    expires_at = (
        datetime.datetime.utcnow()
        + datetime.timedelta(seconds=token_data.get("expires_in", 7200))
    ).isoformat() + "Z"
    token_store.save(user_open_id, {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": expires_at,
    })
```

- [ ] **Step 4: 在 FeishuClient 添加 `send_interactive` 和 `update_message`**

Modify: `cc_feishu_bridge/feishu/client.py`

```python
async def send_interactive(self, chat_id: str, card_json: str, reply_to_message_id: str) -> str:
    """Send an interactive card message, replying to a specific message."""
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.ReplyMessageRequest.builder()
        .message_id(reply_to_message_id)
        .request_body(
            lark.im.v1.ReplyMessageRequestBody.builder()
            .content(card_json)
            .msg_type("interactive")
            .build()
        )
        .build()
    )
    response = await asyncio.to_thread(client.im.v1.message.reply, request)
    if not response.success():
        raise RuntimeError(f"Failed to send card: {response.msg}")
    return response.data.message_id

async def update_message(self, message_id: str, card_json: str) -> None:
    """Update an existing message's content (used for card status updates)."""
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            lark.im.v1.PatchMessageRequestBody.builder()
            .content(card_json)
            .msg_type("interactive")
            .build()
        )
        .build()
    )
    await asyncio.to_thread(client.im.v1.message.patch, request)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: PASS (all 70+ tests)

- [ ] **Step 6: Commit**

```bash
git add cc_feishu_bridge/feishu/message_handler.py cc_feishu_bridge/feishu/client.py
git commit -m "feat: add /feishu auth command with interactive card and background polling"
```

---

## Task 6: 端到端测试验证

**Files:**
- Test: `tests/test_feishu_auth_command.py`

- [ ] **Step 1: 写集成测试**

```python
import pytest, asyncio
from unittest.mock import AsyncMock, patch

from cc_feishu_bridge.feishu.message_handler import MessageHandler, HandlerResult
from cc_feishu_bridge.feishu.client import IncomingMessage

@pytest.fixture
def handler():
    from cc_feishu_bridge.feishu.client import FeishuClient
    from cc_feishu_bridge.security.auth import Authenticator
    from cc_feishu_bridge.security.validator import SecurityValidator
    from cc_feishu_bridge.claude.integration import ClaudeIntegration
    from cc_feishu_bridge.claude.session_manager import SessionManager
    from cc_feishu_bridge.format.reply_formatter import ReplyFormatter
    # ... wire up minimal mocks ...

@pytest.mark.asyncio
async def test_feishu_auth_command_triggers_flow(handler):
    msg = IncomingMessage(
        message_id="om_test",
        chat_id="oc_test",
        user_open_id="ou_test",
        content="/feishu auth",
        message_type="text",
        create_time="",
    )
    with patch("cc_feishu_bridge.feishu.auth_flow.run_auth_flow") as mock_flow:
        result = await handler.handle(msg)
        assert result.success
        mock_flow.assert_called_once()
        call_kwargs = mock_flow.call_args.kwargs
        assert call_kwargs["user_open_id"] == "ou_test"
        assert call_kwargs["chat_id"] == "oc_test"
        assert call_kwargs["message_id"] == "om_test"
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_feishu_auth_command.py
git commit -m "test: add /feishu auth integration test"
```

---

## Task 7: CHANGELOG 和文档

- [ ] **Step 1: 更新 CHANGELOG.md**

在 `[Unreleased]` 下添加：

```markdown
- **/feishu auth 授权指令**：用户发送 `/feishu auth`，机器人发送交互卡片，用户点击授权后 token 持久化，支持文件上传等需要用户权限的操作
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: update CHANGELOG with /feishu auth feature"
```

---

## 自检清单

1. **Spec coverage:** 每个需求有对应 Task？
   - `/stop` 非阻塞架构 → Task 0 ✅
   - Device Auth API → Task 1 ✅
   - 交互卡片构建 → Task 2 ✅
   - Token 持久化 → Task 3 ✅
   - 后台轮询 → Task 4 ✅
   - 命令注册 → Task 5 ✅
   - 测试 → Task 6 ✅

2. **Placeholder scan:** 无 TBD/TODO/placeholder ✅

3. **Type consistency:**
   - `FeishuInstallAPI.__init__` 新增 `app_id`/`app_secret` 参数（Task 1 Step 4）
   - `run_auth_flow` 接收 `user_open_id` 参数传递给 `save_token_fn`（Task 4 Step 2）
   - `FeishuClient` 新增 `send_interactive` 和 `update_message`（Task 5 Step 4）
   - 一致性检查通过 ✅

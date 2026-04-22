# Queue, Reply Chain, Quote Detection, Audio Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对标官方飞书插件：全局消息队列串行处理、AI 回复挂到用户消息下方、支持用户引用消息发送、支持语音消息。

**Architecture:**
- `FeishuClient` 新增 `send_*_reply()` 方法族和 `get_message()` 方法；`IncomingMessage` 新增 `parent_id`、`thread_id` 字段
- `FeishuWSClient` 的事件处理器直接从 event 对象提取 `parent_id`、`thread_id` 并传入 `IncomingMessage`
- `MessageHandler` 重构为队列模式：`handle()` 入队立即返回，`_worker_loop()` 串行出队处理；`_process_message()` 替代原 `handle()` 的核心逻辑；引用检测在 `_process_message()` 中调用 `get_message()` 拼接 prompt；audio 类型媒体走 `_preprocess_media()` 统一处理
- `main.py` 中 `handle_message` 回调逻辑不变，`MessageHandler` 内部已消化队列逻辑

**Tech Stack:** Python 3.11+, asyncio.Queue, lark-oapi SDK

---

## File Map

| 操作 | 文件 |
|------|------|
| 修改 | `cc_feishu_bridge/feishu/client.py` |
| 修改 | `cc_feishu_bridge/feishu/ws_client.py` |
| 修改 | `cc_feishu_bridge/feishu/message_handler.py` |
| 修改 | `cc_feishu_bridge/feishu/media.py` |
| 修改 | `tests/test_feishu_client.py` |
| 修改 | `tests/test_ws_client.py` |
| 新增 | `tests/test_message_handler.py` |

---

## Task 1: IncomingMessage 新增字段 + FeishuClient 新增 API

**Files:**
- Modify: `cc_feishu_bridge/feishu/client.py:11-19`
- Modify: `cc_feishu_bridge/feishu/client.py:304-324`
- Modify: `tests/test_feishu_client.py`

- [ ] **Step 1: 给 `IncomingMessage` dataclass 新增 `parent_id` 和 `thread_id` 字段**

`cc_feishu_bridge/feishu/client.py` 第 11-19 行，将：

```python
@dataclass
class IncomingMessage:
    """Parsed incoming message from Feishu."""
    message_id: str
    chat_id: str
    user_open_id: str
    content: str          # text content
    message_type: str     # "text", "image", "file", etc.
    create_time: str
```

替换为：

```python
@dataclass
class IncomingMessage:
    """Parsed incoming message from Feishu."""
    message_id: str
    chat_id: str
    user_open_id: str
    content: str           # text content
    message_type: str      # "text", "image", "file", "audio", etc.
    create_time: str
    parent_id: str = ""    # 被引用消息的 ID（用户引用/回复某条消息时）
    thread_id: str = ""    # 所在线程的 ID
```

- [ ] **Step 2: `parse_incoming_message()` 提取 `parent_id` 和 `thread_id`**

`cc_feishu_bridge/feishu/client.py` 第 314-321 行，将：

```python
return IncomingMessage(
    message_id=message.get("message_id", ""),
    chat_id=message.get("chat_id", ""),
    user_open_id=sender.get("sender_id", {}).get("open_id", ""),
    content=self._extract_content(message),
    message_type=message.get("msg_type", "text"),
    create_time=message.get("create_time", ""),
)
```

替换为：

```python
return IncomingMessage(
    message_id=message.get("message_id", ""),
    chat_id=message.get("chat_id", ""),
    user_open_id=sender.get("sender_id", {}).get("open_id", ""),
    content=self._extract_content(message),
    message_type=message.get("msg_type", "text"),
    create_time=message.get("create_time", ""),
    parent_id=message.get("parent_id", ""),
    thread_id=message.get("thread_id", ""),
)
```

- [ ] **Step 3: 新增 `send_text_reply()` 方法**

在 `send_interactive()` 方法之后（第 271 行附近），新增：

```python
async def send_text_reply(
    self,
    chat_id: str,
    text: str,
    reply_to_message_id: str,
) -> str:
    """Send a text message as a threaded reply to a specific message."""
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.ReplyMessageRequest.builder()
        .message_id(reply_to_message_id)
        .request_body(
            lark.im.v1.ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}))
            .msg_type("text")
            .build()
        )
        .build()
    )
    response = await asyncio.to_thread(client.im.v1.message.reply, request)
    if not response.success():
        raise RuntimeError(f"Failed to reply: {response.msg}")
    logger.info(f"Replied to {reply_to_message_id} in chat {chat_id}: {response.data.message_id}")
    return response.data.message_id
```

- [ ] **Step 4: 新增 `send_image_reply()` 方法**

在 `send_text_reply()` 之后新增：

```python
async def send_image_reply(
    self,
    chat_id: str,
    image_key: str,
    reply_to_message_id: str,
) -> str:
    """Send an image message as a threaded reply."""
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.ReplyMessageRequest.builder()
        .message_id(reply_to_message_id)
        .request_body(
            lark.im.v1.ReplyMessageRequestBody.builder()
            .content(json.dumps({"image_key": image_key}))
            .msg_type("image")
            .build()
        )
        .build()
    )
    response = await asyncio.to_thread(client.im.v1.message.reply, request)
    if not response.success():
        raise RuntimeError(f"Failed to reply image: {response.msg}")
    return response.data.message_id
```

- [ ] **Step 5: 新增 `send_file_reply()` 方法**

在 `send_image_reply()` 之后新增：

```python
async def send_file_reply(
    self,
    chat_id: str,
    file_key: str,
    file_name: str,
    reply_to_message_id: str,
) -> str:
    """Send a file message as a threaded reply."""
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.ReplyMessageRequest.builder()
        .message_id(reply_to_message_id)
        .request_body(
            lark.im.v1.ReplyMessageRequestBody.builder()
            .content(json.dumps({"file_key": file_key, "file_name": file_name}))
            .msg_type("file")
            .build()
        )
        .build()
    )
    response = await asyncio.to_thread(client.im.v1.message.reply, request)
    if not response.success():
        raise RuntimeError(f"Failed to reply file: {response.msg}")
    return response.data.message_id
```

- [ ] **Step 6: 新增 `get_message()` 方法**

在 `send_text_reply()` 之前（第 104 行附近，`send_text` 方法之后）新增：

```python
async def get_message(self, message_id: str) -> dict | None:
    """Fetch a message by ID. Returns message dict or None on failure."""
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.GetMessageRequest.builder()
        .message_id(message_id)
        .build()
    )
    try:
        response = await asyncio.to_thread(client.im.v1.message.get, request)
        if response.success() and response.data and response.data.items:
            return response.data.items[0]
    except Exception as e:
        logger.warning(f"get_message({message_id}) failed: {e}")
    return None
```

- [ ] **Step 7: 更新测试**

`tests/test_feishu_client.py` 中 `test_parse_incoming_text_message` 的 body 字典新增：

```python
"parent_id": "om_parent_123",
"thread_id": "om_thread_456",
```

并新增断言：

```python
assert msg.parent_id == "om_parent_123"
assert msg.thread_id == "om_thread_456"
```

- [ ] **Step 8: 运行测试验证**

Run: `python -m pytest tests/test_feishu_client.py -v`
Expected: PASS (包括新增的 parent_id/thread_id 断言)

- [ ] **Step 9: Commit**

```bash
git add cc_feishu_bridge/feishu/client.py tests/test_feishu_client.py
git commit -m "feat(client): add parent_id/thread_id to IncomingMessage, add send_*_reply and get_message methods"
```

---

## Task 2: FeishuWSClient 提取 parent_id 和 thread_id

**Files:**
- Modify: `cc_feishu_bridge/feishu/ws_client.py:74-81`
- Modify: `tests/test_ws_client.py`

- [ ] **Step 1: 在 `_build_event_handler()` 中提取 `parent_id` 和 `thread_id`**

`cc_feishu_bridge/feishu/ws_client.py` 第 74-81 行，将：

```python
incoming = IncomingMessage(
    message_id=getattr(message, "message_id", ""),
    chat_id=getattr(message, "chat_id", ""),
    user_open_id=user_open_id,
    content=content,
    message_type=msg_type,
    create_time=getattr(message, "create_time", ""),
)
```

替换为：

```python
incoming = IncomingMessage(
    message_id=getattr(message, "message_id", ""),
    chat_id=getattr(message, "chat_id", ""),
    user_open_id=user_open_id,
    content=content,
    message_type=msg_type,
    create_time=getattr(message, "create_time", ""),
    parent_id=getattr(message, "parent_id", ""),
    thread_id=getattr(message, "thread_id", ""),
)
```

- [ ] **Step 2: 更新测试 mock**

`tests/test_ws_client.py` 的 mock_event 中新增：

```python
mock_event.event.message.parent_id = "om_parent_456"
mock_event.event.message.thread_id = "om_thread_789"
```

新增断言：

```python
assert msg.parent_id == "om_parent_456"
assert msg.thread_id == "om_thread_789"
```

- [ ] **Step 3: 运行测试**

Run: `python -m pytest tests/test_ws_client.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add cc_feishu_bridge/feishu/ws_client.py tests/test_ws_client.py
git commit -m "feat(ws): extract parent_id and thread_id from Feishu event payload"
```

---

## Task 3: MessageHandler 队列重构 + 引用拼接 + 音频支持

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`
- Modify: `cc_feishu_bridge/feishu/media.py`
- New: `tests/test_message_handler.py`

> **注意**：这是改动最大的任务，分 6 个子步骤。

- [ ] **Step 1: 新增 `received_audio` 目录到 `media.py`**

`cc_feishu_bridge/feishu/media.py` 末尾新增：

```python
def make_audio_path(data_dir: str, msg_id: str) -> str:
    """Return the path for saving an inbound audio file."""
    audio_dir = os.path.join(data_dir, "received_audio")
    os.makedirs(audio_dir, exist_ok=True)
    return os.path.join(audio_dir, f"{msg_id}")
```

- [ ] **Step 2: 重构 `MessageHandler.__init__()` — 移除旧字段，新增队列**

将：

```python
self._active_task: asyncio.Task | None = None
self._active_user_id: str | None = None
```

替换为：

```python
self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
self._worker_task: asyncio.Task | None = None
self._current_message_id: str = ""   # 当前处理的消息 ID，用于 reply_to_message_id
```

- [ ] **Step 3: 重构 `handle()` — 入队 + 启动 Worker**

将整个 `handle()` 方法体（第 115-160 行）替换为：

```python
async def handle(self, message: IncomingMessage) -> HandlerResult:
    """将消息入队，立即返回。由 Worker 串行处理。"""
    await self._queue.put(message)
    if self._worker_task is None or self._worker_task.done():
        self._worker_task = asyncio.create_task(self._worker_loop())
    return HandlerResult(success=True)
```

- [ ] **Step 4: 新增 `_worker_loop()` 和 `_process_message()` 方法**

在 `__init__` 之后新增：

```python
async def _worker_loop(self) -> None:
    """串行出队并处理消息。"""
    while True:
        try:
            message = await self._queue.get()
            try:
                self._current_message_id = message.message_id
                await self._process_message(message)
            finally:
                self._current_message_id = ""
                self._queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Worker loop error")

async def _process_message(self, message: IncomingMessage) -> None:
    """处理单条消息：鉴权 → 命令 → 媒体预处理 → 引用检测 → 查询。"""
    auth_result = self.auth.authenticate(message.user_open_id)
    if not auth_result.authorized:
        logger.info(f"Ignoring message from unauthorized user: {message.user_open_id}")
        return

    if message.content.startswith("/") and _is_command(message.content):
        result = await self._handle_command(message)
        if result.response_text:
            await self._safe_send(message.chat_id, message.message_id, result.response_text)
        return

    if message.message_type not in ("text", "image", "file", "audio"):
        await self._safe_send(message.chat_id, message.message_id, "暂不支持该消息类型，请发送文字消息。")
        return

    ok, err = self.validator.validate(message.content)
    if not ok:
        await self._safe_send(message.chat_id, message.message_id, f"⚠️ {err}")
        return

    session = self.sessions.get_active_session(message.user_open_id)
    sdk_session_id = session.sdk_session_id if session else None
    if session and session.chat_id != message.chat_id:
        self.sessions.update_chat_id(message.user_open_id, message.chat_id)

    await self._run_query(message, session, sdk_session_id)
```

- [ ] **Step 5: 重构 `_run_query()` — 使用 reply_to_message_id**

将 `_safe_send(message.chat_id, text)` 全部替换为 `_safe_send(message.chat_id, message.message_id, text)`。

具体修改点：
- 第 247 行 `StreamAccumulator(message.chat_id, self._safe_send)` — 这里 send_fn 需要接收 chat_id 和 text，但 reply 需要 message_id。需要修改 `StreamAccumulator` 初始化方式。

更简洁的做法：让 `StreamAccumulator` 接收 `(chat_id, message_id, text)` 三参数。

将 `StreamAccumulator` 构造函数改为：

```python
def __init__(self, chat_id: str, message_id: str, send_fn, flush_timeout: float = 1.5):
    self.chat_id = chat_id
    self._message_id = message_id
    self._send = send_fn
    self._flush_timeout = flush_timeout
    self._buffer = ""
    self._lock = asyncio.Lock()
    self._timer_task: asyncio.Task | None = None
    self.sent_something = False

async def flush(self) -> None:
    async with self._lock:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        if self._buffer:
            text = self._buffer
            self._buffer = ""
            if text.strip():
                await self._send(self.chat_id, self._message_id, text)
                self.sent_something = True

async def _flush_after(self, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        async with self._lock:
            if self._buffer:
                text = self._buffer
                self._buffer = ""
                if text.strip():
                    await self._send(self.chat_id, self._message_id, text)
                    self.sent_something = True
    except asyncio.CancelledError:
        pass
```

对应地将 `accumulator = StreamAccumulator(message.chat_id, self._safe_send)` 替换为：

```python
accumulator = StreamAccumulator(message.chat_id, message.message_id, self._safe_send)
```

其余 `_safe_send(message.chat_id, text)` 调用（stream_callback 里的）会自动适配，因为 `_safe_send` 签名已更新。

- [ ] **Step 6: 新增引用检测逻辑到 `_run_query()` 中 full_prompt 拼接之前**

在 `full_prompt = ...` 之前（第 262 行附近）新增：

```python
# 3. Resolve quoted message content
quoted_content = ""
if message.parent_id:
    try:
        quoted_msg = await self.feishu.get_message(message.parent_id)
        if quoted_msg:
            sender_name = quoted_msg.get("sender", {}).get("name", "")
            quoted_text = self._extract_quoted_content(quoted_msg)
            if sender_name:
                quoted_content = f"[引用消息: {message.parent_id}] {sender_name}: {quoted_text}"
            else:
                quoted_content = f"[引用消息: {message.parent_id}] {quoted_text}"
            logger.info(f"Quoted message {message.parent_id}: {quoted_text[:100]!r}")
    except Exception:
        logger.warning(f"Failed to fetch quoted message {message.parent_id}")
```

然后将 `full_prompt = ...` 替换为：

```python
quoted_or_media = media_prompt_prefix or quoted_content
full_prompt = (
    f"{quoted_or_media}\n{message.content}".strip()
    if quoted_or_media
    else message.content
)
if quoted_content and media_prompt_prefix:
    full_prompt = f"{media_prompt_prefix}\n{quoted_content}\n{message.content}".strip()
```

（合并两个前缀，按 media → quote → content 顺序排列）

新增辅助方法 `_extract_quoted_content()`（放在 `_safe_send` 之后）：

```python
def _extract_quoted_content(self, message: dict) -> str:
    """Extract text content from a fetched message dict."""
    msg_type = message.get("msg_type", "")
    content_str = message.get("content", "{}")
    try:
        import json
        content = json.loads(content_str)
        if msg_type == "text":
            return content.get("text", "")
        elif msg_type == "post":
            return content.get("text", "")
    except Exception:
        pass
    return str(content_str)
```

- [ ] **Step 7: `_preprocess_media()` 新增 audio 类型处理**

在 `elif message.message_type == "file":` 之后（第 376 行附近）新增：

```python
elif message.message_type == "audio":
    try:
        import json
        content = json.loads(message.content)
        file_key = content.get("file_key", "")
        duration_ms = content.get("duration", 0)
        if not file_key:
            return ""
        from cc_feishu_bridge.feishu.media import make_audio_path, save_bytes
        data = await self.feishu.download_media(msg_id, file_key, msg_type="audio")
        base_path = make_audio_path(data_dir, msg_id)
        save_path = base_path + ".opus"
        save_bytes(save_path, data)
        duration_s = duration_ms / 1000 if duration_ms else None
        duration_str = f" ({duration_s:.1f}s)" if duration_s else ""
        return f"[Audio: {save_path}{duration_str}]"
    except Exception as e:
        logger.warning(f"Failed to process audio message: {e}")
        return ""
```

并将 `_preprocess_media` 的类型检查从 `("image", "file")` 改为 `("image", "file", "audio")`。

- [ ] **Step 8: 重构 `_safe_send()` — 改为 reply 版本**

将：

```python
async def _safe_send(self, chat_id: str, text: str):
    """Send message, ignoring errors (e.g., rate limits)."""
    try:
        await self.feishu.send_text(chat_id, text)
    except Exception as e:
        logger.warning(f"Failed to send message: {e}")
```

替换为：

```python
async def _safe_send(self, chat_id: str, reply_to_message_id: str, text: str):
    """Send a text message as a threaded reply, ignoring errors."""
    try:
        await self.feishu.send_text_reply(chat_id, text, reply_to_message_id)
    except Exception as e:
        logger.warning(f"Failed to send message: {e}")
```

- [ ] **Step 9: 移除旧的 re-entrant 状态字段和相关逻辑**

删除 `__init__` 中的 `self._active_task` 和 `self._active_user_id`。

删除 `_run_query()` finally 块中的：

```python
self._active_task = None
self._active_user_id = None
```

删除 `_handle_stop()` 中的 re-entrant 检查逻辑，改为直接中断 Worker 中的当前任务。

由于队列是串行的，`_handle_stop` 需要能中断正在运行的任务。最好的方式是通过 `asyncio.CancelledError`：取消 `_worker_task`，Worker loop 中的 `task_done()` 在 finally 块中会在任务被取消时正确清理。

将 `_handle_stop()` 改为：

```python
async def _handle_stop(self, message: IncomingMessage) -> HandlerResult:
    """Handle /stop — cancel the current worker task."""
    if self._worker_task is None or self._worker_task.done():
        await self._safe_send(message.chat_id, message.message_id, "当前没有正在运行的查询。")
        return HandlerResult(success=True)
    self._worker_task.cancel()
    self._worker_task = None
    await self._safe_send(message.chat_id, message.message_id, "🛑 已发送停止信号，Claude 将中断当前任务。")
    return HandlerResult(success=True)
```

- [ ] **Step 10: 写测试 `tests/test_message_handler.py`**

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cc_feishu_bridge.feishu.message_handler import MessageHandler, StreamAccumulator, HandlerResult
from cc_feishu_bridge.feishu.client import IncomingMessage


def test_handle_queues_message_and_returns_immediately():
    """handle() should put message in queue and return immediately."""
    handler = _make_handler()
    msg = _text_msg("om_1", "hello")
    result = asyncio.get_event_loop().run_until_complete(handler.handle(msg))
    assert result.success
    assert not handler._queue.empty()


def test_worker_processes_queued_messages_in_order():
    """Messages should be processed FIFO."""
    handler = _make_handler()
    handler.claude.query = AsyncMock(return_value=("response", None, 0.0))
    handler.feishu.get_message = AsyncMock(return_value=None)

    async def run():
        await handler.handle(_text_msg("om_1", "first"))
        await handler.handle(_text_msg("om_2", "second"))
        # Worker processes asynchronously; wait for both to finish
        await asyncio.sleep(0.5)
        calls = handler.claude.query.call_args_list
        assert len(calls) >= 2
        assert "first" in calls[0][1]["prompt"]
        assert "second" in calls[1][1]["prompt"]

    asyncio.get_event_loop().run_until_complete(run())


def test_stream_accumulator_sends_with_message_id():
    """StreamAccumulator should call send_fn with (chat_id, message_id, text)."""
    sent_args = []

    async def capture_send(chat_id, msg_id, text):
        sent_args.append((chat_id, msg_id, text))

    acc = StreamAccumulator("chat_abc", "om_reply_to", capture_send)
    asyncio.get_event_loop().run_until_complete(acc.add_text("hello"))
    asyncio.get_event_loop().run_until_complete(acc.flush())
    assert sent_args == [("chat_abc", "om_reply_to", "hello")]


def test_stop_cancels_worker():
    """Sending /stop should cancel the running worker."""
    handler = _make_handler()
    handler.claude.query = AsyncMock(side_effect=lambda **kw: asyncio.sleep(10))
    handler.feishu.get_message = AsyncMock(return_value=None)

    async def run():
        await handler.handle(_text_msg("om_1", "test"))
        await asyncio.sleep(0.1)
        stop_result = await handler._handle_stop(_text_msg("om_2", "/stop"))
        assert stop_result.success
        assert handler._worker_task is None

    asyncio.get_event_loop().run_until_complete(run())


def _text_msg(msg_id, text):
    return IncomingMessage(
        message_id=msg_id,
        chat_id="chat_abc",
        user_open_id="ou_user1",
        content=text,
        message_type="text",
        create_time="1234567890",
        parent_id="",
        thread_id="",
    )


def _make_handler():
    from cc_feishu_bridge.security.auth import Authenticator
    from cc_feishu_bridge.security.validator import SecurityValidator
    from cc_feishu_bridge.claude.integration import ClaudeIntegration
    from cc_feishu_bridge.claude.session_manager import SessionManager
    from cc_feishu_bridge.format.reply_formatter import ReplyFormatter

    auth = Authenticator(allowed_users=["ou_user1"])
    validator = SecurityValidator(approved_directory="/tmp")
    claude = MagicMock(spec=ClaudeIntegration)
    sm = MagicMock(spec=SessionManager)
    sm.get_active_session.return_value = None
    fm = MagicMock(spec=ReplyFormatter)

    feishu = MagicMock()
    feishu.add_typing_reaction = AsyncMock(return_value="r_123")
    feishu.remove_typing_reaction = AsyncMock()
    feishu.send_text_reply = AsyncMock()

    return MessageHandler(
        feishu_client=feishu,
        authenticator=auth,
        validator=validator,
        claude=claude,
        session_manager=sm,
        formatter=fm,
        approved_directory="/tmp",
        data_dir="/tmp",
    )
```

- [ ] **Step 11: 运行测试**

Run: `python -m pytest tests/test_message_handler.py -v`
Expected: FAIL（缺少 import 或签名不匹配）— 修复直到 PASS

- [ ] **Step 12: Commit**

```bash
git add cc_feishu_bridge/feishu/message_handler.py cc_feishu_bridge/feishu/media.py tests/test_message_handler.py
git commit -m "feat(handler): global queue, reply chain, quote detection, audio support"
```

---

## Task 4: 构建验证

**Files:**
- Modify: `build_cli.py`（不改动，用现有脚本构建）

- [ ] **Step 1: 重新构建 CLI**

Run: `. .venv/bin/activate && python build_cli.py 2>&1 | tail -10`
Expected: `Build SUCCEEDED: dist/cc-feishu-bridge`

- [ ] **Step 2: 验证 --help 正常运行**

Run: `./dist/cc-feishu-bridge --help`
Expected: usage 输出，无报错

- [ ] **Step 3: 运行全部测试**

Run: `python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: 无 regression failures（允许已知失败）

---

## Self-Review Checklist

- [ ] `IncomingMessage.parent_id` 在 `client.py` 和 `ws_client.py` 两处同时更新
- [ ] `send_text_reply`、`send_image_reply`、`send_file_reply` 均使用 `lark.im.v1.ReplyMessageRequest`
- [ ] `StreamAccumulator` 接收 `(chat_id, message_id, send_fn)` 三参数，所有 flush 调用传入正确 message_id
- [ ] `_safe_send` 签名从 `(chat_id, text)` 改为 `(chat_id, reply_to_message_id, text)`
- [ ] `full_prompt` 拼接顺序：media_prefix → quoted_content → 用户实际消息内容
- [ ] audio 类型加入 `message_type` 允许列表
- [ ] `_preprocess_media` 处理 `("image", "file", "audio")`
- [ ] `/stop` 取消 `_worker_task`，不清空队列
- [ ] Worker loop 中 `finally` 包含 `task_done()` 和重置 `_current_message_id`

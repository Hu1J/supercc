# WebSocket 长连接重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将 cc-feishu-bridge 从 Webhook 模式重构为 WebSocket 长连接模式（程序主动连飞书，无需端口暴露），支持目录级数据隔离（`.cc-feishu/`）、Claude cwd 使用 session.project_path、以及进程管理 CLI（list/stop）。

**架构：** 用 `lark_oapi.ws.Client` 建立与飞书的长连接，通过 `EventDispatcherHandlerBuilder` 注册 `im.message.receive_v1` 事件回调。配置和数据存储在启动目录的 `.cc-feishu/` 子目录下，实现天然多开隔离。

**技术栈：** lark-oapi ws.Client、asyncio、SQLite

---

## 文件变更概览

| 文件 | 改动 |
|------|------|
| `src/main.py` | 删除 aiohttp server；WS 长连接启动；`.cc-feishu/` 目录级配置；list/stop 子命令 |
| `src/feishu/ws_client.py` | 新建：封装 ws.Client，提供消息回调接口 |
| `src/feishu/message_handler.py` | cwd 改为 session.project_path |
| `src/config.py` | 新增 `resolve_config_path()` — 查找 `.cc-feishu/config.yaml` |

---

## Task 1: 新建 FeishuWSClient

**文件：**
- 创建: `src/feishu/ws_client.py`
- 测试: `tests/test_ws_client.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_ws_client.py
import pytest
from unittest.mock import AsyncMock, MagicMock

def test_ws_client_initializes():
    from src.feishu.ws_client import FeishuWSClient
    client = FeishuWSClient(
        app_id="test_app_id",
        app_secret="test_secret",
        on_message=AsyncMock(),
    )
    assert client.app_id == "test_app_id"
    assert client.app_secret == "test_secret"
    assert client._handler is not None
    assert client._ws_client is None  # not started yet

def test_on_message_callback():
    from src.feishu.ws_client import FeishuWSClient
    cb = AsyncMock()
    client = FeishuWSClient(app_id="id", app_secret="secret", on_message=cb)

    # Mock event object simulating lark event
    mock_event = MagicMock()
    mock_event.message.message_id = "msg_123"
    mock_event.message.chat_id = "chat_abc"
    mock_event.message.msg_type = "text"
    mock_event.message.content = '{"text":"hello"}'
    mock_event.message.create_time = "1234567890"

    mock_sender = MagicMock()
    mock_sender.sender_id.open_id = "user_xyz"
    mock_event.sender = mock_sender

    client._handle_p2p_message(mock_event)
    cb.assert_called_once()
    msg = cb.call_args[0][0]
    assert msg.message_id == "msg_123"
    assert msg.chat_id == "chat_abc"
    assert msg.user_open_id == "user_xyz"
    assert msg.content == "hello"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ws_client.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 FeishuWSClient**

```python
# src/feishu/ws_client.py
"""Feishu WebSocket long-connection client using lark-oapi ws.Client."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable
from unittest.mock import MagicMock

from src.feishu.client import IncomingMessage

logger = logging.getLogger(__name__)

MessageCallback = Callable[[IncomingMessage], Awaitable[None]]


class FeishuWSClient:
    """Manages WebSocket connection to Feishu via lark-oapi ws.Client."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        bot_name: str = "Claude",
        domain: str = "feishu",
        on_message: MessageCallback | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_name = bot_name
        self.domain = domain
        self._on_message = on_message
        self._ws_client = None
        self._handler = None

    def _build_event_handler(self):
        """Build EventDispatcherHandler with p2p message callback registered."""
        import lark_oapi as lark

        builder = lark.EventDispatcherHandler.builder(
            encrypt_key="",  # 长连接模式不需要 encrypt
            verification_token="",
        )

        def wrapped_handler(event):
            """Handle incoming p2p message event."""
            if self._on_message is None:
                return
            try:
                message = event.message
                sender = event.sender
                msg_type = getattr(message, "msg_type", "text")
                content_str = getattr(message, "content", "{}")

                # Parse JSON content for text messages
                content = content_str
                if msg_type == "text":
                    try:
                        import json
                        content = json.loads(content_str).get("text", "")
                    except Exception:
                        pass

                sender_id = getattr(sender, "sender_id", None)
                user_open_id = ""
                if sender_id is not None:
                    user_open_id = getattr(sender_id, "open_id", "")

                incoming = IncomingMessage(
                    message_id=getattr(message, "message_id", ""),
                    chat_id=getattr(message, "chat_id", ""),
                    user_open_id=user_open_id,
                    content=content,
                    message_type=msg_type,
                    create_time=getattr(message, "create_time", ""),
                )
                asyncio.ensure_future(self._on_message(incoming))
            except Exception as e:
                logger.exception(f"Error handling Feishu message: {e}")

        builder.register_p2_im_message_receive_v1(wrapped_handler)
        self._handler = builder.build()
        return self._handler

    def start(self) -> None:
        """Start the WebSocket long connection (blocking)."""
        import lark_oapi as lark

        handler = self._build_event_handler()
        base_url = "https://open.feishu.cn" if self.domain == "feishu" else "https://open.larksuite.com"

        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=handler,
            domain=base_url,
            auto_reconnect=True,
        )
        logger.info(f"Starting Feishu WebSocket connection to {base_url}...")
        self._ws_client.start()

    # Expose handler for testing
    def _handle_p2p_message(self, event):
        """Internal handler for testing — calls the wrapped handler directly."""
        handler = self._build_event_handler()
        handler._processorMap.get("p2.im.message.receive_v1")._func(event)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ws_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_ws_client.py src/feishu/ws_client.py
git commit -m "feat: add FeishuWSClient for WebSocket long connection"
```

---

## Task 2: 实现目录级配置路径 + CLI 子命令解析

**文件：**
- 创建: `src/config.py` (修改现有)
- 测试: `tests/test_config_path.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_config_path.py
import os
import tempfile
from pathlib import Path

def test_resolve_config_path_creates_cc_dir():
    """resolve_config_path creates .cc-feishu/ in cwd if not exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        from src.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == f"{tmpdir}/.cc-feishu/config.yaml"
        assert data_dir == f"{tmpdir}/.cc-feishu"
        assert Path(cfg).exists()

def test_resolve_config_path_resumes_existing():
    """If .cc-feishu/config.yaml exists, returns it (auto-resume)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cc_dir = Path(tmpdir) / ".cc-feishu"
        cc_dir.mkdir()
        cfg_file = cc_dir / "config.yaml"
        cfg_file.write_text("feishu:\n  app_id: test\n")

        os.chdir(tmpdir)
        from src.config import resolve_config_path
        cfg, data_dir = resolve_config_path()

        assert cfg == str(cfg_file)
        assert data_dir == str(cc_dir)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_config_path.py -v`
Expected: FAIL (resolve_config_path not defined)

- [ ] **Step 3: 实现 resolve_config_path**

在 `src/config.py` 末尾添加：

```python
def resolve_config_path() -> tuple[str, str]:
    """Resolve config and data directories relative to cwd.

    Uses .cc-feishu/ subdirectory in the current working directory
    for natural multi-instance isolation:
      - Config: {cwd}/.cc-feishu/config.yaml
      - Data:  {cwd}/.cc-feishu/ (sessions.db, logs)

    Auto-creates .cc-feishu/ if not found (runs install flow on first start).
    """
    cwd = os.getcwd()
    cc_dir = Path(cwd) / ".cc-feishu"
    cc_dir.mkdir(exist_ok=True)
    return (str(cc_dir / "config.yaml"), str(cc_dir))
```

注意需要 `import os` 在 config.py 顶部。

同时在 `src/config.py` 的 `Config` dataclass 中，新增一个字段用于存储 `data_dir`（由 resolve_config_path 返回），修改 `load_config` 使其接受可选的 `data_dir` 参数。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_config_path.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config_path.py
git commit -m "feat: add resolve_config_path for directory-level data isolation"
```

---

## Task 3: 重构 main.py — WS 长连接 + 目录级配置 + list/stop 子命令

**文件：**
- 修改: `src/main.py`

- [ ] **Step 1: 写测试**

```python
# tests/test_main_ws.py
import pytest
import argparse
from unittest.mock import patch

def test_main_shows_no_config_message(capsystem, tmp_path, monkeypatch):
    """Without config, shows message to use install flow."""
    monkeypatch.chdir(tmp_path)
    # Just verify main() doesn't crash on missing config
    from src.main import main
    with pytest.raises(SystemExit):
        main(["--log-level", "DEBUG"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_main_ws.py -v`
Expected: FAIL

- [ ] **Step 3: 实现重构后的 main.py**

完整重写 `src/main.py`：

```python
"""CLI entry point — starts WebSocket long connection to Feishu.

Data is stored in .cc-feishu/ subdirectory of the current working directory,
enabling natural multi-instance isolation.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from src.config import load_config, resolve_config_path
from src.feishu.client import FeishuClient, IncomingMessage
from src.feishu.ws_client import FeishuWSClient
from src.feishu.message_handler import MessageHandler
from src.security.auth import Authenticator
from src.security.validator import SecurityValidator
from src.claude.integration import ClaudeIntegration
from src.claude.session_manager import SessionManager
from src.format.reply_formatter import ReplyFormatter

import logging

logger = logging.getLogger(__name__)


def create_handler(config, data_dir: str) -> MessageHandler:
    """Create MessageHandler with all dependencies wired up."""
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
    )
    authenticator = Authenticator(allowed_users=config.auth.allowed_users)
    validator = SecurityValidator(approved_directory=config.claude.approved_directory)
    claude = ClaudeIntegration(
        cli_path=config.claude.cli_path,
        max_turns=config.claude.max_turns,
        approved_directory=config.claude.approved_directory,
    )
    db_path = os.path.join(data_dir, "sessions.db")
    session_manager = SessionManager(db_path=db_path)
    formatter = ReplyFormatter()

    handler = MessageHandler(
        feishu_client=feishu,
        authenticator=authenticator,
        validator=validator,
        claude=claude,
        session_manager=session_manager,
        formatter=formatter,
        approved_directory=config.claude.approved_directory,
    )
    return handler


async def handle_message(message: IncomingMessage, handler: MessageHandler) -> None:
    """Callback for incoming Feishu messages — dispatch to handler."""
    try:
        await handler.handle(message)
    except Exception as e:
        logger.exception(f"Error handling message: {e}")


def write_pid(pid_file: str) -> None:
    """Write current PID to file."""
    Path(pid_file).write_text(str(os.getpid()))


def remove_pid(pid_file: str) -> None:
    """Remove PID file."""
    Path(pid_file).unlink(missing_ok=True)


def start_bridge(config_path: str, data_dir: str) -> None:
    """Start the bridge: load config and run WebSocket connection."""
    config = load_config(config_path)
    handler = create_handler(config, data_dir)

    ws_client = FeishuWSClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
        domain=config.feishu.domain,
        on_message=lambda msg: handle_message(msg, handler),
    )

    # Write PID file for process management
    pid_file = os.path.join(data_dir, "cc-feishu-bridge.pid")
    write_pid(pid_file)

    # Clean up PID file on exit
    def cleanup(signum, frame):
        remove_pid(pid_file)
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    logger.info(f"Starting Feishu bridge (WS mode) — data: {data_dir}")
    ws_client.start()


def list_bridges() -> None:
    """List all cc-feishu-bridge instances by scanning .cc-feishu/*.pid files."""
    print("\nRunning cc-feishu-bridge instances:")
    print(f"{'PID':<8} {'Directory':<40} {'PID File':<50}")
    print("-" * 100)

    found = False
    for root, dirs, files in os.walk("."):
        # Only look in .cc-feishu directories
        if ".cc-feishu" not in dirs:
            continue
        cc_dir = os.path.join(root, ".cc-feishu")
        pid_file = os.path.join(cc_dir, "cc-feishu-bridge.pid")
        if not os.path.exists(pid_file):
            continue
        try:
            pid = int(Path(pid_file).read_text().strip())
            # Check if process is alive
            try:
                os.kill(pid, 0)
                status = "running"
            except OSError:
                status = "dead (clean up pid file)"
            print(f"{pid:<8} {os.path.abspath(root):<40} {pid_file:<50} {status}")
            found = True
        except (ValueError, OSError):
            pass

    if not found:
        print("No running instances found.")
    print()


def stop_bridge(pid: int) -> None:
    """Stop a cc-feishu-bridge instance by PID."""
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped PID {pid}")
    except OSError as e:
        print(f"Failed to stop PID {pid}: {e}")


def detect_config() -> bool:
    """Check if .cc-feishu/config.yaml exists in cwd."""
    cfg, _ = resolve_config_path()
    return Path(cfg).exists()


async def interactive_install() -> None:
    """Run the QR-code install flow, then start bridge."""
    from src.install.flow import run_install_flow
    cfg_path, data_dir = resolve_config_path()
    await run_install_flow(cfg_path)
    # Install complete; start bridge in a fresh loop.
    start_bridge(cfg_path, data_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Feishu Bridge — data stored in .cc-feishu/"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # start (default)
    start_parser = subparsers.add_parser("start", help="Start the bridge (default)")

    # list
    list_parser = subparsers.add_parser("list", help="List all running instances")

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop a running instance")
    stop_parser.add_argument("pid", type=int, help="PID of the instance to stop")

    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("qrcode").setLevel(logging.WARNING)

    command = args.command

    if command == "list":
        list_bridges()
        return

    if command == "stop":
        stop_bridge(args.pid)
        return

    # Default: start
    if detect_config():
        _, data_dir = resolve_config_path()
        log_file = os.path.join(data_dir, "cc-feishu-bridge.log")
        Path(data_dir).mkdir(exist_ok=True)
        # Add file handler
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        logger.info(f"Config found, starting bridge...")
        start_bridge(*resolve_config_path())
    else:
        logger.info("No config found, running install flow...")
        asyncio.run(interactive_install())


if __name__ == "__main__":
    main()
```

注意：`resolve_config_path()` 已经被 import 了，需要确保 `src/config.py` 里已经添加了 `import os`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/main.py
git commit -m "refactor: replace webhook with ws.Client + add process management CLI"
```

---

## Task 4: 更新 message_handler — 使用 session.project_path 作为 cwd

**文件：**
- 修改: `src/feishu/message_handler.py:81`

- [ ] **Step 1: 确认改动位置**

确认第 81 行附近：
```python
response, new_session_id, cost = await self.claude.query(
    prompt=message.content,
    session_id=sdk_session_id,
    cwd=self.approved_directory,   # ← 改这里
    on_stream=stream_callback,
)
```

- [ ] **Step 2: 改为 session.project_path**

```python
response, new_session_id, cost = await self.claude.query(
    prompt=message.content,
    session_id=sdk_session_id,
    cwd=session.project_path if session else self.approved_directory,
    on_stream=stream_callback,
)
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/feishu/message_handler.py
git commit -m "fix: use session.project_path as Claude cwd"
```

---

## Task 5: 构建验证

- [ ] **Step 1: 构建**

Run: `python build.py --clean && ls -lh dist/cc-feishu-bridge`
Expected: ~41MB binary

- [ ] **Step 2: Commit（无变更则跳过）**

---

## Task 6: 端到端测试

- [ ] **Step 1: 在测试目录运行**

```bash
mkdir -p /tmp/test-cc && cd /tmp/test-cc
/tmp/cc-feishu-bridge  # should start install flow since no .cc-feishu/
```

- [ ] **Step 2: 启动后在新终端验证 list**

```bash
cd /tmp/test-cc && ./cc-feishu-bridge list
# Should show the running instance
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore: complete WS refactor + process management"
```

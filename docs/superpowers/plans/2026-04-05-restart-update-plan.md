# restart / update 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `restart` 和 `update` 两个子命令（CLI + 飞书消息指令），支持热重启和热升级。

**Architecture:** `restarter.py` 与 `switcher.py` 完全对称；`run_restart_cli()` / `run_restart()` 接收一个可选的 `file_lock` 参数用于释放锁；`update` 在进程内执行 pip install，成功后调用 restart 逻辑；所有卡片在 `os._exit(0)` 之前发完。

**Tech Stack:** Python 标准库（`os._exit`, `subprocess`, `signal`）+ 现有 `switcher.py` 模式 + `packaging.version` 做版本比较。

---

## 文件结构

| 文件 | 操作 |
|------|------|
| `cc_feishu_bridge/restarter.py` | 新建 |
| `cc_feishu_bridge/main.py` | 修改：新增子命令、导出 `_active_lock` |
| `cc_feishu_bridge/feishu/message_handler.py` | 修改：新增 `/restart` `/update` 路由 |
| `tests/test_restarter.py` | 新建 |
| `README.md` | 修改：命令列表 |
| `CHANGELOG.md` | 修改：版本条目 |

---

## Task 1: 创建 `restarter.py` — 骨架与异常定义

**Files:**
- Create: `cc_feishu_bridge/restarter.py`
- Test: `tests/test_restarter.py`

- [ ] **Step 1: 创建 `restarter.py` 骨架文件**

```python
"""Restart and update — hot restart / hot upgrade for cc-feishu-bridge."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cc_feishu_bridge.feishu.client import FeishuClient


class RestartError(Exception): pass
class StartupTimeoutError(RestartError): pass
```

- [ ] **Step 2: 定义 `RestartStep` dataclass（与 `SwitchStep` 完全一致）**

```python
@dataclass
class RestartStep:
    step: int          # 1-based
    total: int         # total steps (4)
    label: str         # short label for CLI
    status: str        # "done" | "final" | "error"
    detail: str = ""
    success: bool = False
    new_pid: Optional[int] = None
```

- [ ] **Step 3: 定义步骤标签常量（CLI 和飞书消息两套）**

```python
_CLI_STEP_LABELS = [
    "准备重启",
    "启动新 bridge",
    "等待新进程就绪",
    "重启完成",
]

_FEISHU_STEP_LABELS = [
    "🛑 准备重启",
    "🚀 启动新 bridge",
    "⏳ 等待新进程就绪",
    "✅ 重启完成",
]
```

- [ ] **Step 4: 定义内部工具函数 `_pid_file_path`、`_read_pid`、`_is_process_alive`、`_start_bridge`**

直接从 `switcher.py` 复制对应函数，调整函数名。`_start_bridge()` 与 `switcher._start_bridge()` 完全一致（都是 subprocess.Popen `cc-feishu-bridge start`）。

- [ ] **Step 5: 提交**

```bash
git add cc_feishu_bridge/restarter.py
git commit -m "feat: add restarter.py skeleton with RestartStep and helper functions"
```

---

## Task 2: 实现 `restart_to()` generator

**Files:**
- Modify: `cc_feishu_bridge/restarter.py`

- [ ] **Step 1: 实现 `_restart_to(file_lock=None)` generator**

`_restart_to` 接收可选的 `file_lock: filelock.FileLock | None` 参数，流程如下：

```python
def _restart_to(file_lock=None):
    """Restart bridge in the current directory.

    Args:
        file_lock: FileLock object acquired by main.py; released before
                   starting new process so the new instance can acquire it.
    """
    current_path = os.getcwd()

    # Step 1: 准备重启
    yield RestartStep(step=1, total=4, label=_CLI_STEP_LABELS[0], status="done")

    # Step 2: 释放 FileLock（如果有），然后启动新进程
    if file_lock is not None:
        file_lock.release()

    new_pid = _start_bridge(current_path)

    # Step 3: 等待新进程 pid file 出现
    yield RestartStep(step=3, total=4, label=_CLI_STEP_LABELS[2], status="done")

    # Step 4: 重启完成
    yield RestartStep(
        step=4, total=4, label=_CLI_STEP_LABELS[3],
        status="final", detail=f"新 PID {new_pid}",
        success=True, new_pid=new_pid,
    )
```

注意：Step 2（启动新 bridge）和 Step 3（等待就绪）合并为一个 yield，因为 `_start_bridge` 内部已经等待 pid file 出现才返回。

- [ ] **Step 2: 提交**

```bash
git commit -m "feat: add _restart_to() generator"
```

---

## Task 3: 实现 `run_restart()` 和 `run_restart_cli()`

**Files:**
- Modify: `cc_feishu_bridge/restarter.py`

- [ ] **Step 1: 实现 `run_restart(file_lock, feishu, chat_id, reply_to_message_id)`**

与 `switcher.run_switch()` 完全一致，使用 `send_interactive_reply` 发卡片。`_restart_to` 传入 `file_lock` 参数。

```python
async def run_restart(file_lock, feishu: "FeishuClient",
                      chat_id: str, reply_to_message_id: str) -> None:
    current_path = os.getcwd()
    total = 4
    for step_obj in _restart_to(file_lock=file_lock):
        bar = "▓" * step_obj.step + "░" * (total - step_obj.step)
        label = _FEISHU_STEP_LABELS[step_obj.step - 1]
        if step_obj.status == "final":
            card = f"## ✅ 重启完成\n\n**当前目录**: `{current_path}`\n**新进程 PID**: `{step_obj.new_pid}`\n\n🎉 Bridge 已重启，可以在飞书中继续对话了。"
            await feishu.send_interactive_reply(chat_id, card, reply_to_message_id)
        else:
            card = f"## 🔄 正在重启\n\n**当前目录**: `{current_path}`\n\n{bar} `{step_obj.step}/{total}` {label}\n\n⏳ 即将重启，请稍候..."
            await feishu.send_interactive_reply(chat_id, card, reply_to_message_id)
```

- [ ] **Step 2: 实现 `run_restart_cli(file_lock, feishu=None, chat_id=None)`**

与 `switcher.run_switch_cli()` 完全一致的异步 generator 模式，用 `asyncio.new_event_loop()` + `asyncio.set_event_loop(loop)` 驱动。当 Feishu 不可用时，直接 yield `_restart_to(file_lock)` 的结果。

```python
def run_restart_cli(file_lock, feishu=None, chat_id: str | None = None):
    import asyncio

    async def _run():
        if not feishu or not chat_id:
            for step in _restart_to(file_lock=file_lock):
                yield step
            return
        async def _send(card_md: str):
            try:
                await feishu.send_interactive_reply(chat_id, card_md, "")
            except Exception:
                pass
        # 初始卡片
        initial = f"## 🔄 正在重启\n\n⏳ 准备重启，请稍候..."
        await _send(initial)
        for step_obj in _restart_to(file_lock=file_lock):
            bar = "▓" * step_obj.step + "░" * (4 - step_obj.step)
            label = _FEISHU_STEP_LABELS[step_obj.step - 1]
            if step_obj.status == "final":
                card = f"## ✅ 重启完成\n\n🎉 Bridge 已重启，可以在飞书中继续对话了。"
                await _send(card)
            else:
                card = f"## 🔄 正在重启\n\n{bar} `{step_obj.step}/4` {label}\n\n⏳ 即将重启，请稍候..."
                await _send(card)
            yield step_obj

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        gen = _run()
        try:
            while True:
                yielded = loop.run_until_complete(gen.__anext__())
                yield yielded
        except StopAsyncIteration:
            pass
    finally:
        loop.close()
```

- [ ] **Step 3: 提交**

```bash
git commit -m "feat: add run_restart() and run_restart_cli()"
```

---

## Task 4: 实现 `check_version()` 和 `do_update()` / `run_update()` / `run_update_cli()`

**Files:**
- Modify: `cc_feishu_bridge/restarter.py`

- [ ] **Step 1: 实现 `check_version()` — 获取 PyPI 最新版本**

使用 `subprocess.run(["pip", "index", "versions", "cc-feishu-bridge"], capture_output=True, text=True)` 解析输出。用 `packaging.version` 比较。失败时抛异常让调用方捕获。

```python
import packaging.version

def check_version() -> tuple[str, str]:
    """Return (current_version, latest_version).

    Raises RestartError on failure.
    """
    from cc_feishu_bridge import __version__ as current_ver

    try:
        result = subprocess.run(
            ["pip", "index", "versions", "cc-feishu-bridge"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RestartError("pip index versions failed")
        # Output format: "cc-feishu-bridge (X.Y.Z)"
        import re
        m = re.search(r"cc-feishu-bridge\s+\((.+?)\)", result.stdout)
        if not m:
            raise RestartError("无法解析 pip index versions 输出")
        latest_ver = m.group(1)
        return (current_ver, latest_ver)
    except subprocess.TimeoutExpired:
        raise RestartError("检查版本超时")
    except Exception as e:
        raise RestartError(f"检查版本失败: {e}")
```

注意：需要确认 `cc_feishu_bridge/__init__.py` 中导出了 `__version__`，如果还没有就加上。

- [ ] **Step 2: 定义 `UpdateStep` dataclass**

```python
@dataclass
class UpdateStep:
    step: int
    total: int        # 7 steps
    label: str
    status: str       # "done" | "final" | "skip" (already latest)
    detail: str = ""
    success: bool = False
    new_pid: Optional[int] = None
```

步骤映射：

| step | label (CLI) | label (飞书) |
|------|-------------|-------------|
| 1 | 检查更新 | 📋 检查更新 |
| 2 | 下载新版本 | ⬇️ 下载新版本 |
| 3 | 下载完成 | ✅ 下载完成 |
| 4 | 准备重启 | 🔄 准备重启 |
| 5 | 启动新 bridge | 🚀 启动新 bridge |
| 6 | 等待新进程就绪 | ⏳ 等待新进程就绪 |
| 7 | 重启完成 | ✅ 重启完成 |

- [ ] **Step 3: 实现 `_do_update(file_lock=None)` generator**

```python
_UPDATE_CLI_STEP_LABELS = [...]
_UPDATE_FEISHU_STEP_LABELS = [...]

def _do_update(file_lock=None):
    """Check version, install update if needed, restart.

    Yields UpdateStep. On "already latest", yields step 1 and step 2 with status="skip".
    """
    current_path = os.getcwd()

    # Step 1: 检查更新
    yield UpdateStep(step=1, total=7, label=_UPDATE_CLI_STEP_LABELS[0], status="done")
    current_ver, latest_ver = check_version()
    if packaging.version.parse(latest_ver) <= packaging.version.parse(current_ver):
        yield UpdateStep(
            step=2, total=7,
            label=_UPDATE_CLI_STEP_LABELS[1],
            status="skip",
            detail=f"当前版本 {current_ver} 已是最新",
            success=True,
        )
        return

    # Step 2: 下载新版本
    yield UpdateStep(step=2, total=7, label=_UPDATE_CLI_STEP_LABELS[1], status="done",
                     detail=f"{current_ver} → {latest_ver}")
    try:
        subprocess.run(
            ["pip", "install", "-U", "cc-feishu-bridge",
             "-i", "https://pypi.org/simple/"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RestartError("下载超时")
    except Exception as e:
        raise RestartError(f"pip install 失败: {e}")

    # Step 3: 下载完成
    yield UpdateStep(step=3, total=7, label=_UPDATE_CLI_STEP_LABELS[2], status="done")

    # Step 4-7: 复用 _restart_to 的步骤（step 偏移 3）
    for restart_step in _restart_to(file_lock=file_lock):
        yield UpdateStep(
            step=restart_step.step + 3,
            total=7,
            label=_UPDATE_CLI_STEP_LABELS[restart_step.step + 2],
            status=restart_step.status,
            detail=restart_step.detail,
            success=restart_step.success,
            new_pid=restart_step.new_pid,
        )
```

- [ ] **Step 4: 实现 `run_update()` 和 `run_update_cli()`**

与 `run_restart()` / `run_restart_cli()` 结构完全一致。飞书卡片区分"已是最新"和正常流程。"已是最新"只发 skip 那张卡然后结束，不走 restart 步骤。

```python
async def run_update(file_lock, feishu: "FeishuClient",
                     chat_id: str, reply_to_message_id: str) -> None:
    current_path = os.getcwd()
    for step in _do_update(file_lock=file_lock):
        if step.status == "skip":
            card = f"## ✅ 已是最新版本\n\n**当前版本**: `{step.detail}`\n\n无需更新，继续使用吧。"
            await feishu.send_interactive_reply(chat_id, card, reply_to_message_id)
            return
        # ... 同 run_restart() 的发卡片逻辑，7 步，进度条
```

```python
def run_update_cli(file_lock, feishu=None, chat_id: str | None = None):
    # 结构与 run_restart_cli() 完全一致
    ...
```

- [ ] **Step 5: 提交**

```bash
git commit -m "feat: add check_version, do_update, run_update, run_update_cli"
```

---

## Task 5: `main.py` 新增 restart / update 子命令并导出 `_active_lock`

**Files:**
- Modify: `cc_feishu_bridge/main.py`

- [ ] **Step 1: 在 `main.py` 顶部添加 `_active_lock: filelock.FileLock | None = None` 模块级变量**

```python
_active_lock: "filelock.FileLock | None" = None
```

- [ ] **Step 2: 在 `start_bridge()` 中，获取 lock 后赋值给模块变量**

在 `lock = filelock.FileLock(...)` 之后，`lock.acquire()` 之前添加：

```python
    global _active_lock
    _active_lock = lock
```

- [ ] **Step 3: 在子命令解析部分，新增 `restart_parser` 和 `update_parser`**

```python
restart_parser = subparsers.add_parser("restart", help="Restart current bridge instance")
update_parser = subparsers.add_parser("update", help="Check for updates and restart if needed")
```

- [ ] **Step 4: 在命令分发部分，新增 `restart` 和 `update` 处理**

```python
if command == "restart":
    from cc_feishu_bridge.restarter import run_restart_cli, RestartError as RestartErr
    try:
        for step in run_restart_cli(_active_lock):
            bar = "━" * (step.step - 1) + "▓" + "░" * (step.total - step.step)
            if step.status == "final":
                print(f"\r[{bar}] ✓ {step.label} {step.detail}")
            else:
                print(f"\r[{bar}] {step.label}...")
        print()
        # All cards sent — now exit
        import os as _os
        _os._exit(0)
    except RestartErr as e:
        print(f"\n❌ 重启失败: {e}")
        sys.exit(1)
    return

if command == "update":
    from cc_feishu_bridge.restarter import run_update_cli, RestartError as RestartErr
    try:
        for step in run_update_cli(_active_lock):
            bar = "━" * (step.step - 1) + "▓" + "░" * (step.total - step.step)
            if step.status == "skip":
                print(f"✅ {step.label} {step.detail}")
                return
            if step.status == "final":
                print(f"\r[{bar}] ✓ {step.label} {step.detail}")
            else:
                print(f"\r[{bar}] {step.label}...")
        print()
        _os._exit(0)
    except RestartErr as e:
        print(f"\n❌ 更新失败: {e}")
        sys.exit(1)
    return
```

注意：`import os as _os` 放在 if 块内部避免作用域问题，或者在文件顶部已有 `import os`，直接用 `os._exit(0)` 即可。

- [ ] **Step 5: 提交**

```bash
git commit -m "feat: add restart and update CLI subcommands to main.py"
```

---

## Task 6: `message_handler.py` 新增 `/restart` 和 `/update` 路由

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: 在 `_handle_switch` 之后添加 `_handle_restart`**

```python
async def _handle_restart(self, message: IncomingMessage) -> HandlerResult:
    from cc_feishu_bridge.restarter import run_restart, RestartError
    from cc_feishu_bridge.main import _active_lock

    await self.feishu.add_typing_reaction(message.message_id)
    try:
        await run_restart(_active_lock, self.feishu, message.chat_id, message.message_id)
    except RestartError as e:
        await self._safe_send(
            message.chat_id, message.message_id,
            f"❌ 重启失败: {e}"
        )
    import os as _os
    _os._exit(0)
```

- [ ] **Step 2: 在 `_handle_switch` 之后添加 `_handle_update`**

```python
async def _handle_update(self, message: IncomingMessage) -> HandlerResult:
    from cc_feishu_bridge.restarter import run_update, RestartError
    from cc_feishu_bridge.main import _active_lock

    await self.feishu.add_typing_reaction(message.message_id)
    try:
        await run_update(_active_lock, self.feishu, message.chat_id, message.message_id)
    except RestartError as e:
        await self._safe_send(
            message.chat_id, message.message_id,
            f"❌ 更新失败: {e}"
        )
    import os as _os
    _os._exit(0)
```

- [ ] **Step 3: 在 `handle_message()` 的路由分发中添加 `/restart` 和 `/update`**

找到 `if cmd == "/switch"` 的判断，在其后添加：

```python
if cmd == "/restart":
    return await _handle_restart(self, message)
if cmd == "/update":
    return await _handle_update(self, message)
```

- [ ] **Step 4: 提交**

```bash
git commit -m "feat: add /restart and /update message handlers"
```

---

## Task 7: 写 `test_restarter.py`

**Files:**
- Create: `tests/test_restarter.py`

- [ ] **Step 1: 写 `check_version()` 测试**

```python
from cc_feishu_bridge.restarter import check_version

def test_check_version_returns_tuple():
    current, latest = check_version()
    assert isinstance(current, str)
    assert isinstance(latest, str)
    assert len(latest.split(".")) == 3
```

- [ ] **Step 2: 写 `RestartStep` dataclass 测试**

```python
from cc_feishu_bridge.restarter import RestartStep

def test_restart_step_fields():
    step = RestartStep(step=1, total=4, label="准备重启", status="done")
    assert step.step == 1
    assert step.total == 4
    assert step.success is False
    assert step.new_pid is None

def test_restart_step_final():
    step = RestartStep(step=4, total=4, label="重启完成", status="final",
                       success=True, new_pid=12345)
    assert step.success is True
    assert step.new_pid == 12345
```

- [ ] **Step 3: 写 `_restart_to()` 测试（mock `_start_bridge`）**

```python
from unittest.mock import patch
from cc_feishu_bridge.restarter import _restart_to

def test_restart_to_yields_4_steps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cc_dir = tmp_path / ".cc-feishu-bridge"
    cc_dir.mkdir()

    with patch("cc_feishu_bridge.restarter._start_bridge", return_value=99999):
        steps = list(_restart_to(file_lock=None))

    assert len(steps) == 4
    assert steps[0].step == 1
    assert steps[3].status == "final"
    assert steps[3].new_pid == 99999
    assert steps[3].success is True

def test_restart_to_releases_lock(tmp_path, monkeypatch):
    from filelock import FileLock
    monkeypatch.chdir(tmp_path)
    cc_dir = tmp_path / ".cc-feishu-bridge"
    cc_dir.mkdir()
    lock_file = cc_dir / ".lock"
    lock = FileLock(str(lock_file))
    lock.acquire()

    released = []
    def mock_release():
        released.append(True)
    lock.release = mock_release

    with patch("cc_feishu_bridge.restarter._start_bridge", return_value=11111):
        list(_restart_to(file_lock=lock))

    assert released == [True]
```

- [ ] **Step 4: 写 `_do_update()` 测试**

```python
from unittest.mock import patch
from cc_feishu_bridge.restarter import _do_update

def test_already_latest_yields_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cc_dir = tmp_path / ".cc-feishu-bridge"
    cc_dir.mkdir()

    with patch("cc_feishu_bridge.restarter.check_version",
               return_value=("99.99.99", "99.99.99")):
        steps = list(_do_update(file_lock=None))

    assert len(steps) == 2
    assert steps[1].status == "skip"
    assert "已是最新" in steps[1].detail
```

- [ ] **Step 5: 运行测试确认全部通过**

```bash
pytest tests/test_restarter.py -v
```

- [ ] **Step 6: 提交**

```bash
git add tests/test_restarter.py
git commit -m "test: add test_restarter.py"
```

---

## Task 8: 更新 README 和 CHANGELOG

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: README.md — 命令列表新增 `/restart` 和 `/update`**

在命令列表中添加：

```
- `/restart` — 重启当前 bridge 实例
- `/update` — 检查 PyPI 最新版本，如有更新则自动下载并重启
```

- [ ] **Step 2: CHANGELOG.md — 新增 `[Unreleased]` 区块**

在文件顶部（在 `## [0.2.6]` 之前）添加：

```markdown
## [Unreleased]

### Added
- **`cc-feishu-bridge restart`**：重启当前目录的 bridge 实例，所有通知卡片在退出前发完
- **`cc-feishu-bridge update`**：检查 PyPI 最新版本，有更新则下载并自动 restart
- **`/restart` 飞书指令**：与 CLI 命令行为一致，支持飞书端热重启
- **`/update` 飞书指令**：飞书端热升级，步骤：检查更新 → 下载 → 重启
```

- [ ] **Step 3: 提交**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: update README and CHANGELOG for restart/update"
```

---

## 自检清单

- [ ] Spec 中每个功能点都有对应 Task：restart CLI ✓、restart 飞书 ✓、update CLI ✓、update 飞书 ✓
- [ ] 无 "TBD" / "TODO" / "fill in later" 等占位符
- [ ] `check_version()` 异常时有 fallback，不静默崩溃
- [ ] `_active_lock` 在 `start_bridge()` 外部默认为 None，不影响正常启动
- [ ] `os._exit(0)` 在所有 restart/update 成功路径的最后执行
- [ ] `test_restarter.py` 所有测试通过

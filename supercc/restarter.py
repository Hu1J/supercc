"""Restart and update — hot restart / hot upgrade for supercc."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from supercc.adapter.feishu.client import FeishuClient


class RestartError(Exception): pass
class StartupTimeoutError(RestartError): pass


# Step labels for CLI display (short, single line)
_CLI_STEP_LABELS = [
    "准备重启",
    "清理文件锁",
    "启动新实例",
    "检查新实例",
    "重启完成",
]

# Step labels for Feishu messages (detailed, emoji)
_FEISHU_STEP_LABELS = [
    "🛑 准备重启",
    "🧹 清理文件锁",
    "🚀 启动新实例",
    "🔍 检查新实例",
    "✅ 重启完成",
]


@dataclass
class RestartStep:
    """A single step in the restart process, yielded as it happens."""
    step: int          # 1–5
    total: int         # always 5
    label: str         # short label shown to user
    status: str        # "done" | "error" | "final"
    detail: str = ""   # extra info (PID, path, etc.)
    success: bool = False   # True only on the final step on success
    new_pid: Optional[int] = None  # available on the final step


@dataclass
class RestartResult:
    success: bool
    new_pid: Optional[int] = None


def _pid_file_path(project_path: str) -> str:
    """Return the PID file path for a project."""
    return os.path.join(project_path, ".supercc", "supercc.pid")


def _read_pid(pid_file: str) -> Optional[int]:
    """Read PID from file. Returns None if file doesn't exist or is invalid."""
    if not os.path.exists(pid_file):
        return None
    try:
        return int(Path(pid_file).read_text().strip())
    except (ValueError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_process(pid: int, sig: int, timeout: float) -> bool:
    """Send signal to process and wait for it to die. Returns True if process stopped."""
    try:
        os.kill(pid, sig)
    except OSError:
        return True  # Process already dead

    # Wait for process to die
    start = time.time()
    while time.time() - start < timeout:
        if not _is_process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _stop_bridge(project_path: str) -> bool:
    """Stop the SuperCC instance for a project. Uses SIGTERM then SIGKILL. Returns True if stopped, False if failed."""
    pid_file = _pid_file_path(project_path)
    pid = _read_pid(pid_file)

    if pid is None:
        return True  # Already stopped

    # SIGTERM first
    if not _kill_process(pid, signal.SIGTERM, timeout=5.0):
        # SIGKILL if still alive
        if not _kill_process(pid, signal.SIGKILL, timeout=2.0):
            return False

    # Clean up pid file
    try:
        Path(pid_file).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _restart_to(file_lock=None, package: str = "supercc"):
    """Restart SuperCC in the current directory.

    Args:
        file_lock: FileLock object acquired by main.py; released before
                   starting new process so the new instance can acquire it.
        package: Package name to restart (determines binary).
    Yields RestartStep objects (5 steps total).
    """
    current_path = os.getcwd()
    data_dir = os.path.join(current_path, ".supercc")
    pid_file = os.path.join(data_dir, "supercc.pid")
    instance_lock = os.path.join(data_dir, ".instance.lock")

    # Step 1: 准备重启
    yield RestartStep(step=1, total=5, label=_CLI_STEP_LABELS[0], status="done")

    # Step 2: 释放文件锁 + 删除 pid 文件
    if file_lock is not None:
        file_lock.release()
    Path(pid_file).unlink(missing_ok=True)

    # 检查确认两个文件都没了
    if os.path.exists(pid_file) or os.path.exists(instance_lock):
        raise RestartError("文件锁/pid 文件未成功清理，无法重启")

    yield RestartStep(step=2, total=5, label=_CLI_STEP_LABELS[1], status="done")

    # Step 3: 启动新实例
    new_pid = _start_bridge(current_path, package=package)
    yield RestartStep(step=3, total=5, label=_CLI_STEP_LABELS[2], status="done")

    # Step 4: 检查新实例已成功启动（pid 文件 + filelock 都存在）
    if not (os.path.exists(pid_file) and os.path.exists(instance_lock)):
        raise StartupTimeoutError("新实例未成功启动")
    yield RestartStep(step=4, total=5, label=_CLI_STEP_LABELS[3], status="done")

    # Step 5: 重启完成（自我 exit 由调用方处理，消息不展示）
    yield RestartStep(
        step=5, total=5, label=_CLI_STEP_LABELS[4],
        status="final", detail=f"新 PID {new_pid}",
        success=True, new_pid=new_pid,
    )


async def run_restart(file_lock, feishu: "FeishuClient",
                      chat_id: str, reply_to_message_id: str) -> None:
    """Run the restart with detailed step-by-step Feishu notifications.

    Sends a rich progress card to Feishu, updating it as each step completes.
    """
    current_path = os.getcwd()
    total = 5

    for step_obj in _restart_to(file_lock=file_lock):
        bar = "▓" * step_obj.step + "░" * (total - step_obj.step)
        label = _FEISHU_STEP_LABELS[step_obj.step - 1] if step_obj.step <= len(_FEISHU_STEP_LABELS) else f"步骤 {step_obj.step}"

        if step_obj.status == "final":
            final_card = (
                f"## ✅ 重启完成\n\n"
                f"**当前目录**: `{current_path}`\n"
                f"**新进程 PID**: `{step_obj.new_pid}`\n\n"
                f"🎉 SuperCC 已重启，可以在飞书中继续对话了。"
            )
            await feishu.send_interactive_reply(chat_id, final_card, reply_to_message_id)
        else:
            progress_card = (
                f"## 🔄 正在重启\n\n"
                f"**当前目录**: `{current_path}`\n\n"
                f"{bar} `{step_obj.step}/{total}` {label}\n\n"
                f"⏳ 即将重启，请稍候..."
            )
            await feishu.send_interactive_reply(chat_id, progress_card, reply_to_message_id)


def run_restart_cli(file_lock, feishu=None, chat_id: str | None = None):
    """CLI version of restart — yields RestartStep, optionally sends Feishu notifications.

    Args:
        file_lock: FileLock object acquired by main.py
        feishu: FeishuClient instance (optional, for notifications)
        chat_id: Feishu chat_id (optional, required if feishu is provided)
    """
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
                pass  # non-fatal, CLI continues

        # Initial card
        initial = f"## 🔄 正在重启\n\n⏳ 准备重启，请稍候..."
        await _send(initial)

        for step_obj in _restart_to(file_lock=file_lock):
            bar = "▓" * step_obj.step + "░" * (5 - step_obj.step)
            label = _FEISHU_STEP_LABELS[step_obj.step - 1]

            if step_obj.status == "final":
                card = (
                    f"## ✅ 重启完成\n\n"
                    f"**当前目录**: `{os.getcwd()}`\n"
                    f"**新进程 PID**: `{step_obj.new_pid}`\n\n"
                    f"🎉 SuperCC 已重启，可以在飞书中继续对话了。"
                )
                await _send(card)
            else:
                card = (
                    f"## 🔄 正在重启\n\n"
                    f"{bar} `{step_obj.step}/5` {label}\n\n"
                    f"⏳ 即将重启，请稍候..."
                )
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


def _start_bridge(project_path: str, package: str = "supercc", timeout: float = 60.0) -> int:
    """Start the SuperCC instance for project using subprocess.Popen with start_new_session=True.

    Args:
        project_path: Path to the project directory.
        package: Package name to start (determines binary).
        timeout: Timeout in seconds.

    Returns the PID of the started process.
    Raises StartupTimeoutError if pid file doesn't appear within timeout.

    Note: caller is responsible for cleaning up stale pid/lock files before calling.
    """
    data_dir = os.path.join(project_path, ".supercc")
    pid_file = os.path.join(data_dir, "supercc.pid")

    stdout_log = open(os.path.join(data_dir, "bridge-stdout.log"), "w")
    stderr_log = open(os.path.join(data_dir, "bridge-stderr.log"), "w")
    try:
        proc = subprocess.Popen(
            [package, "start"],
            cwd=project_path,
            stdin=subprocess.DEVNULL,
            stdout=stdout_log,
            stderr=stderr_log,
            start_new_session=True,
        )

        # Wait for pid file to appear
        start = time.time()
        while time.time() - start < timeout:
            pid = _read_pid(pid_file)
            if pid is not None:
                stdout_log.close()
                stderr_log.close()
                return pid
            # Check if process crashed
            if proc.poll() is not None:
                stdout_log.close()
                stderr_log.close()
                raise StartupTimeoutError(f"SuperCC process exited unexpectedly during startup")
            time.sleep(0.2)

        stdout_log.close()
        stderr_log.close()
        raise StartupTimeoutError(
            f"PID file did not appear within {timeout}s after starting SuperCC"
        )
    except Exception:
        stdout_log.close()
        stderr_log.close()
        raise


# ---------------------------------------------------------------------------
# Update / hot-upgrade support
# ---------------------------------------------------------------------------

def _get_package_name() -> str:
    """Get the current package name from pyproject.toml."""
    import yaml
    try:
        with open(Path(__file__).resolve().parent.parent / "pyproject.toml") as f:
            return yaml.safe_load(f)["project"]["name"]
    except Exception:
        return "pysupercc"  # fallback


def check_version() -> tuple[str, str]:
    """Check current vs latest version of the running package via PyPI JSON API.

    Returns (current_version, latest_version).
    Raises RestartError on any failure.
    """
    import httpx
    from supercc import __version__ as current_ver
    package = _get_package_name()
    try:
        response = httpx.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=15,
        )
        response.raise_for_status()
        latest_ver = response.json()["info"]["version"]
        return (current_ver, latest_ver)
    except httpx.HTTPStatusError as e:
        raise RestartError(f"PyPI 请求失败: {e.response.status_code}")
    except Exception as e:
        raise RestartError(f"检查版本失败: {e}")


# Step labels for update CLI display
_UPDATE_CLI_STEP_LABELS = [
    "检查更新", "检查新版本", "下载完成",
    "准备重启", "清理文件锁", "启动新实例", "检查新实例", "重启完成",
]

# Step labels for update Feishu messages
_UPDATE_FEISHU_STEP_LABELS = [
    "📋 检查更新", "📦 检查新版本", "✅ 下载完成",
    "🛑 准备重启", "🧹 清理文件锁", "🚀 启动新实例", "🔍 检查新实例", "✅ 重启完成",
]


@dataclass
class UpdateStep:
    """A single step in the update process, yielded as it happens."""
    step: int          # 1–8
    total: int         # always 8
    label: str         # short label shown to user
    status: str        # "done" | "final" | "skip"
    detail: str = ""   # extra info
    success: bool = False
    new_pid: Optional[int] = None


def _do_update(file_lock=None):
    """Check version, install update if needed, restart.

    Yields UpdateStep.
    """
    import packaging.version

    # Step 1: 检查更新
    current_ver, latest_ver = check_version()
    yield UpdateStep(
        step=1, total=8,
        label=_UPDATE_CLI_STEP_LABELS[0],
        status="done",
        detail=f"{current_ver} → {latest_ver}",
    )

    has_update = packaging.version.parse(latest_ver) > packaging.version.parse(current_ver)

    if has_update:
        package = "pysupercc"
        yield UpdateStep(step=2, total=8, label=_UPDATE_CLI_STEP_LABELS[1], status="done",
                        detail=f"{current_ver} → {latest_ver}")
        _pip_install(package)
    else:
        # 已是最新，无需更新
        yield UpdateStep(
            step=2, total=8,
            label=_UPDATE_CLI_STEP_LABELS[1],
            status="skip",
            detail=current_ver,
            success=True,
        )
        return

    # Step 3: 下载完成
    yield UpdateStep(step=3, total=8, label=_UPDATE_CLI_STEP_LABELS[2], status="done")

    # Step 4-8: 复用 _restart_to（偏移 3）
    for restart_step in _restart_to(file_lock=file_lock, package=package):
        yield UpdateStep(
            step=restart_step.step + 3,
            total=8,
            label=_UPDATE_CLI_STEP_LABELS[restart_step.step + 2],
            status=restart_step.status,
            detail=restart_step.detail,
            success=restart_step.success,
            new_pid=restart_step.new_pid,
        )


def _pip_install(package: str) -> None:
    """Install a package via pip. Raises RestartError on failure."""
    try:
        result = subprocess.run(
            ["pip", "install", "-U", package, "-i", "https://pypi.org/simple/"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RestartError(f"pip install 失败: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        raise RestartError("下载超时")
    except Exception as e:
        raise RestartError(f"pip install 失败: {e}")


async def run_update(file_lock, feishu: "FeishuClient",
                     chat_id: str, reply_to_message_id: str) -> bool:
    """Run the update with detailed step-by-step Feishu notifications.

    Sends a rich progress card to Feishu, updating it as each step completes.
    When status == "skip" (already latest), sends an "already latest" card and returns.

    Returns:
        True if an actual update (pip install) was performed, False if already latest (skipped).
    """
    import logging
    logger = logging.getLogger(__name__)

    current_path = os.getcwd()
    total = 8

    for step_obj in _do_update(file_lock=file_lock):
        if step_obj.status == "skip":
            card = (
                f"## ✅ 已是最新版本\n\n"
                f"**当前版本**: `{step_obj.detail}`\n\n"
                f"无需更新，继续使用吧 🎉"
            )
            await feishu.send_interactive_reply(chat_id, card, reply_to_message_id)
            return False

        bar = "▓" * step_obj.step + "░" * (total - step_obj.step)
        label = (_UPDATE_FEISHU_STEP_LABELS[step_obj.step - 1]
                 if step_obj.step <= len(_UPDATE_FEISHU_STEP_LABELS)
                 else f"步骤 {step_obj.step}")

        if step_obj.status == "final":
            final_card = (
                f"## ✅ 更新完成\n\n"
                f"**当前目录**: `{current_path}`\n"
                f"**新进程 PID**: `{step_obj.new_pid}`\n\n"
                f"🎉 SuperCC 已更新，可以在飞书中继续对话了。"
            )
            await feishu.send_interactive_reply(chat_id, final_card, reply_to_message_id)
        else:
            detail_line = (
                f"**版本**: `{step_obj.detail}`\n\n"
                if step_obj.detail else ""
            )
            progress_card = (
                f"## 🔄 正在更新\n\n"
                f"**当前目录**: `{current_path}`\n\n"
                f"{detail_line}"
                f"{bar} `{step_obj.step}/{total}` {label}\n\n"
                f"⏳ 正在更新，请稍候..."
            )
            await feishu.send_interactive_reply(chat_id, progress_card, reply_to_message_id)
    return True


def run_update_cli(file_lock, feishu=None, chat_id: str | None = None):
    """CLI version of update — yields UpdateStep, optionally sends Feishu notifications.

    Args:
        file_lock: FileLock object acquired by main.py
        feishu: FeishuClient instance (optional, for notifications)
        chat_id: Feishu chat_id (optional, required if feishu is provided)

    When status == "skip", sends "already latest" card and returns immediately
    without sending progress cards.
    """
    import asyncio
    import logging
    logger = logging.getLogger(__name__)

    async def _run():
        if not feishu or not chat_id:
            for step in _do_update(file_lock=file_lock):
                yield step
            return

        async def _send(card_md: str):
            try:
                await feishu.send_interactive_reply(chat_id, card_md, "")
            except Exception:
                pass  # non-fatal, CLI continues

        # Materialize steps to check final status before sending any cards
        steps = list(_do_update(file_lock=file_lock))

        if steps and steps[-1].status == "skip":
            # Already latest
            initial = f"## 🔄 正在更新\n\n⏳ 检查更新，请稍候..."
            await _send(initial)
            step1_detail = next(
                (s.detail for s in steps if s.step == 1 and s.detail),
                steps[-1].detail
            )
            card = (
                f"## ✅ 已是最新版本\n\n"
                f"**当前版本**: `{step1_detail}`\n\n"
                f"无需更新，继续使用吧 🎉"
            )
            await _send(card)
            return

        # Normal update flow: send initial card then process each step
        initial = f"## 🔄 正在更新\n\n⏳ 检查更新，请稍候..."
        await _send(initial)

        for step_obj in steps:
            bar = "▓" * step_obj.step + "░" * (8 - step_obj.step)
            label = (_UPDATE_FEISHU_STEP_LABELS[step_obj.step - 1]
                     if step_obj.step <= len(_UPDATE_FEISHU_STEP_LABELS)
                     else f"步骤 {step_obj.step}")

            if step_obj.status == "final":
                card = (
                    f"## ✅ 更新完成\n\n"
                    f"**当前目录**: `{os.getcwd()}`\n"
                    f"**新进程 PID**: `{step_obj.new_pid}`\n\n"
                    f"🎉 SuperCC 已更新，可以在飞书中继续对话了。"
                )
                await _send(card)
            else:
                detail_line = (
                    f"**版本**: `{step_obj.detail}`\n\n"
                    if step_obj.detail else ""
                )
                card = (
                    f"## 🔄 正在更新\n\n"
                    f"{detail_line}"
                    f"{bar} `{step_obj.step}/8` {label}\n\n"
                    f"⏳ 正在更新，请稍候..."
                )
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

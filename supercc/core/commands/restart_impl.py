"""Restart and update — hot restart / hot upgrade for supercc.

原 supercc/restarter.py，移入 core/commands/ 作为 core 正式模块。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from supercc.channels.feishu.client import FeishuClient


class RestartError(Exception): pass
class StartupTimeoutError(RestartError): pass


def _build_restart_argv(event: str) -> list[str]:
    """Build argv for restart/update subprocess (POSIX). Windows uses Popen separately."""
    from pathlib import Path
    supercc_bin = str(Path(sys.executable).parent / "supercc")
    if event == "update":
        return [supercc_bin, "update"]
    return [supercc_bin, "gateway", "run"]


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


def _restart_to(package: str = "supercc"):
    """Restart SuperCC in the current directory.

    Args:
        package: Package name to restart (determines binary).
    Yields RestartStep objects (5 steps total).
    """
    current_path = os.getcwd()
    data_dir = os.path.join(current_path, ".supercc")
    pid_file = os.path.join(data_dir, "supercc.pid")
    instance_lock = os.path.join(data_dir, ".instance.lock")

    # Step 1: 准备重启
    yield RestartStep(step=1, total=5, label=_CLI_STEP_LABELS[0], status="done", detail=current_path)

    # Step 2: 清理文件锁 + pid 文件
    # 直接 unlink 锁文件，让新实例启动时自己创建并持有新锁。
    # 如果当前进程持有 OS 级 flock，unlink 后新实例仍能获取新锁（不同 inode）。
    Path(instance_lock).unlink(missing_ok=True)
    Path(pid_file).unlink(missing_ok=True)

    # 注意：不做 exists 检查，因为存在 TOCTTU 竞态：
    # unlink 和 exists 检查之间，另一进程可能创建新文件。
    # 新实例启动时会自己检查并覆盖，不依赖这里的检查。

    yield RestartStep(step=2, total=5, label=_CLI_STEP_LABELS[1], status="done", detail=current_path)

    # Step 3: 启动新实例
    new_pid = _start_bridge(current_path, package=package)
    yield RestartStep(step=3, total=5, label=_CLI_STEP_LABELS[2], status="done", detail=current_path)

    # Step 4: 检查新实例已成功启动（pid 文件存在）
    if not os.path.exists(pid_file):
        raise StartupTimeoutError("新实例未成功启动")
    yield RestartStep(step=4, total=5, label=_CLI_STEP_LABELS[3], status="done", detail=current_path)

    # Step 5: 重启完成（自我 exit 由调用方处理，消息不展示）
    yield RestartStep(
        step=5, total=5, label=_CLI_STEP_LABELS[4],
        status="final", detail=f"新 PID {new_pid}",
        success=True, new_pid=new_pid,
    )


async def run_restart(feishu: "FeishuClient",
                      chat_id: str, reply_to_message_id: str):
    """Run the restart with detailed step-by-step Feishu notifications.

    Yields RestartStep objects. Caller must iterate with `async for` to execute the generator.
    """
    current_path = os.getcwd()
    total = 5

    for step_obj in _restart_to():
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

        yield step_obj


def run_restart_cli(feishu=None, chat_id: str | None = None, project_path: str | None = None):
    """CLI version of restart — yields RestartStep, optionally sends Feishu notifications.

    Args:
        feishu: FeishuClient instance (optional, for notifications)
        chat_id: Feishu chat_id (optional, required if feishu is provided)
        project_path: Target project directory to switch to before restarting.
                      If None, restarts in current directory.
    """
    import asyncio

    async def _run():
        if project_path:
            os.chdir(project_path)

        if not feishu or not chat_id:
            for step in _restart_to():
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

        for step_obj in _restart_to():
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
        package: Unused, kept for backward compat.
        timeout: Timeout in seconds.

    Returns the PID of the started process.
    Raises StartupTimeoutError if pid file doesn't appear within timeout.

    Note: caller is responsible for cleaning up stale pid/lock files before calling.
    """
    data_dir = os.path.join(project_path, ".supercc")
    pid_file = os.path.join(data_dir, "supercc.pid")

    stdout_log = open(os.path.join(data_dir, "supercc-stdout.log"), "w")
    stderr_log = open(os.path.join(data_dir, "supercc-stderr.log"), "w")
    # Hardcode supercc — migration is done, pip package name no longer matters here
    # 跨平台：Unix 用 start_new_session，Windows 用 CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    if sys.platform == "win32":
        import subprocess as _subprocess
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            ["supercc", "start"],
            cwd=project_path,
            stdin=subprocess.DEVNULL,
            stdout=stdout_log,
            stderr=stderr_log,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )
    else:
        proc = subprocess.Popen(
            ["supercc", "start"],
            cwd=project_path,
            stdin=subprocess.DEVNULL,
            stdout=stdout_log,
            stderr=stderr_log,
            start_new_session=True,
        )
    # 立即关闭 parent 侧句柄，交给 OS 异步释放
    # Windows 上旧进程被 kill 后句柄释放较慢，等待会导致 "file in use"
    stdout_log.close()
    stderr_log.close()

    # Wait for pid file to appear
    start = time.time()
    while time.time() - start < timeout:
        pid = _read_pid(pid_file)
        if pid is not None:
            return pid
        # Check if process crashed
        if proc.poll() is not None:
            raise StartupTimeoutError(f"SuperCC process exited unexpectedly during startup")
        time.sleep(0.2)

    raise StartupTimeoutError(
        f"PID file did not appear within {timeout}s after starting SuperCC"
    )


# ---------------------------------------------------------------------------
# Update / hot-upgrade support
# ---------------------------------------------------------------------------

def _get_package_name() -> str:
    """Get the current package name from pyproject.toml."""
    import yaml
    try:
        with open(Path(__file__).resolve().parent.parent / "pyproject.toml") as f:
            return yaml.safe_load(f)["project"]["name"]
    except (KeyError, FileNotFoundError, PermissionError, TypeError):
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


def do_update() -> tuple[str, str | None]:
    """检查版本，有更新则 pip install。返回 (current_ver, latest_ver or None)。"""
    import packaging.version

    current_ver, latest_ver = check_version()
    has_update = packaging.version.parse(latest_ver) > packaging.version.parse(current_ver)
    if not has_update:
        return current_ver, None

    _pip_install("pysupercc")
    return current_ver, latest_ver


def _pip_install(package: str) -> None:
    """Install a package via pip. Raises RestartError on failure."""
    import sys
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", package, "-i", "https://pypi.org/simple/"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RestartError(f"pip install 失败: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        raise RestartError("下载超时")
    except Exception as e:
        raise RestartError(f"pip install 失败: {e}")


def _cleanup_and_replace(event: str, project_path: str = "") -> None:
    """清理 PID 文件，然后替换当前进程。

    POSIX:  os.execvp 原地替换，同 PID。
    Windows: subprocess.Popen + sys.exit()（execvp 在 Windows 上不能处理 .cmd/.exe）

    注意：本函数不返回。execvp 成功后当前进程内存被新镜像替换。
    """
    if project_path:
        os.chdir(project_path)

    data_dir = os.path.join(os.getcwd(), ".supercc")
    pid_file = os.path.join(data_dir, "supercc.pid")

    # unlink PID 文件
    Path(pid_file).unlink(missing_ok=True)

    # 统一用 execvp 原地替换进程，不管 daemon 模式。
    # daemon 模式下 launchd/systemd 只看进程存活，execvp 同 PID 替换不会触发服务重启。
    argv = _build_restart_argv(event)

    if sys.platform == "win32":
        import subprocess as _subprocess
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        _subprocess.Popen(
            argv,
            cwd=os.getcwd(),
            stdin=_subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )
        sys.exit(0)
    else:
        # 关闭 stdin，避免新进程意外继承
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)

        os.execvp(argv[0], argv)
        # ← 这行之后不返回。execvp 替换当前进程，新镜像接管同一 PID。

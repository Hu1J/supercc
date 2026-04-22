"""Project switcher — stops current bridge, starts target bridge with rewritten config."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:
    from cc_feishu_bridge.feishu.client import FeishuClient


class SwitchError(Exception): pass
class TargetStopError(SwitchError): pass
class CurrentStopError(SwitchError): pass
class StartupTimeoutError(SwitchError): pass


# Step labels for CLI display (short, single line)
_CLI_STEP_LABELS = [
    "停止目标 bridge",
    "拷贝 config.yaml",
    "启动目标 bridge",
    "确认目标 bridge 运行中",
    "停止当前 bridge",
]

# Step labels for Feishu messages (detailed, emoji)
_FEISHU_STEP_LABELS = [
    "🛑 停止目标 bridge 进程",
    "📋 拷贝 config.yaml 至目标目录",
    "🚀 在目标目录启动 bridge",
    "✅ 确认目标 bridge 运行正常",
    "🛑 关闭当前 bridge",
]


@dataclass
class SwitchStep:
    """A single step in the switch process, yielded as it happens."""
    step: int          # 1–5
    total: int         # always 5
    label: str         # short label shown to user
    status: str        # "done" | "error" | "final"
    detail: str = ""   # extra info (PID, path, etc.)
    success: bool = False   # True only on the final step on success
    target_pid: Optional[int] = None  # available on the final step


@dataclass
class SwitchResult:
    success: bool
    target_path: str
    target_pid: Optional[int] = None


def _pid_file_path(project_path: str) -> str:
    """Return the PID file path for a project."""
    return os.path.join(project_path, ".cc-feishu-bridge", "cc-feishu-bridge.pid")


def _config_file_path(project_path: str) -> str:
    """Return the config file path for a project."""
    return os.path.join(project_path, ".cc-feishu-bridge", "config.yaml")


def _target_config_file_path(project_path: str) -> str:
    """Return the target config file path (where we write the copied config)."""
    return _config_file_path(project_path)


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
    """Stop the bridge for a project. Uses SIGTERM then SIGKILL. Returns True if stopped, False if failed."""
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



def _copy_and_fix_config(current_path: str, target_path: str) -> bool:
    """Read current config.yaml, rewrite storage.db_path and claude.approved_directory to target, write to target.

    Returns True if config was copied, False if current project has no config (skip copy).
    """
    current_config_path = _config_file_path(current_path)
    target_config_path = _target_config_file_path(target_path)

    # No config in current project — skip copy, target must have/manage its own
    if not os.path.exists(current_config_path):
        return False

    with open(current_config_path) as f:
        raw = yaml.safe_load(f)

    # Rewrite storage.db_path to target's sessions.db
    target_sessions_db = os.path.join(
        target_path, ".cc-feishu-bridge", "sessions.db"
    )
    if "storage" not in raw:
        raw["storage"] = {}
    raw["storage"]["db_path"] = target_sessions_db

    # Rewrite claude.approved_directory to target path
    if "claude" not in raw:
        raw["claude"] = {}
    raw["claude"]["approved_directory"] = target_path

    # Ensure .cc-feishu-bridge dir exists in target
    Path(target_config_path).parent.mkdir(parents=True, exist_ok=True)

    with open(target_config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)
    return True


def _start_bridge(target_path: str, timeout: float = 8.0) -> int:
    """Start the bridge for target project using subprocess.Popen with start_new_session=True.

    Returns the PID of the started process.
    Raises StartupTimeoutError if pid file doesn't appear within timeout.
    """
    pid_file = _pid_file_path(target_path)

    # Remove stale pid file if exists
    Path(pid_file).unlink(missing_ok=True)

    # Start bridge via the installed binary (works for both pip installs and
    # PyInstaller binaries — cc-feishu-bridge is in PATH in both cases)
    target_cc = os.path.join(target_path, ".cc-feishu-bridge")
    stdout_log = open(os.path.join(target_cc, "bridge-stdout.log"), "w")
    stderr_log = open(os.path.join(target_cc, "bridge-stderr.log"), "w")
    proc = subprocess.Popen(
        ["cc-feishu-bridge", "start"],
        cwd=target_path,
        stdout=stdout_log,
        stderr=stderr_log,
        start_new_session=True,
    )

    # Wait for pid file to appear
    start = time.time()
    while time.time() - start < timeout:
        pid = _read_pid(pid_file)
        if pid is not None:
            return pid
        # Check if process crashed
        if proc.poll() is not None:
            raise StartupTimeoutError(f"Bridge process exited unexpectedly during startup")
        time.sleep(0.2)

    raise StartupTimeoutError(
        f"PID file did not appear within {timeout}s after starting bridge"
    )


def switch_to(target_path: str):
    """Execute the full project switch flow, yielding SwitchStep as each step completes.

    Steps (stops on error, no rollback):
    1. Stop target bridge (if running)
    2. Copy config.yaml to target (rewrite storage.db_path) — skipped if current project not initialized
    3. Start bridge in target
    4. Verify target bridge is running
    5. Stop current bridge

    Yields SwitchStep for each step. Caller prints/logs/sends Feishu messages.
    Raises SwitchError on fatal failure.
    """
    current_path = os.getcwd()

    # Step 1: Stop target bridge
    yield SwitchStep(step=1, total=5, label=_CLI_STEP_LABELS[0], status="done")
    if not _stop_bridge(target_path):
        raise TargetStopError(f"无法停止目标 bridge")

    # Step 2: Copy and fix config.yaml (skipped if current project not initialized)
    yield SwitchStep(step=2, total=5, label=_CLI_STEP_LABELS[1], status="done")
    try:
        copied = _copy_and_fix_config(current_path, target_path)
    except Exception as e:
        raise SwitchError(f"无法拷贝配置文件到目标: {e}")

    # Step 3: Start target bridge
    yield SwitchStep(step=3, total=5, label=_CLI_STEP_LABELS[2], status="done")
    target_pid: Optional[int] = None
    try:
        target_pid = _start_bridge(target_path)
    except StartupTimeoutError as e:
        raise StartupTimeoutError(f"目标 bridge 启动超时: {e}")

    # Step 4: Verify target bridge is running
    yield SwitchStep(step=4, total=5, label=_CLI_STEP_LABELS[3], status="done", detail=f"PID {target_pid}")

    # Step 5: Stop current bridge
    yield SwitchStep(step=5, total=5, label=_CLI_STEP_LABELS[4], status="done")
    if not _stop_bridge(current_path):
        raise CurrentStopError(f"无法停止当前 bridge")

    yield SwitchStep(step=5, total=5, label="切换完成", status="final",
                     detail=f"新 bridge PID {target_pid}", success=True, target_pid=target_pid)

    return SwitchResult(success=True, target_path=target_path, target_pid=target_pid)


async def run_switch(target_path: str, feishu: "FeishuClient",
                     chat_id: str, reply_to_message_id: str) -> None:
    """Run the switch with detailed step-by-step Feishu notifications.

    Sends a rich progress card to Feishu, updating it as each step completes.
    """
    current_path = os.getcwd()
    total = 5

    for step_obj in switch_to(target_path):
        bar = "▓" * step_obj.step + "░" * (total - step_obj.step)
        label = _FEISHU_STEP_LABELS[step_obj.step - 1] if step_obj.step <= len(_FEISHU_STEP_LABELS) else f"步骤 {step_obj.step}"

        if step_obj.status == "final":
            final_card = (
                f"## ✅ 切换完成\n\n"
                f"**目标项目**: `{target_path}`\n"
                f"**新进程 PID**: `{step_obj.target_pid}`\n\n"
                f"🎉 飞书消息流已切换到目标项目，继续对话吧！\n\n"
                f"返回时执行 `/switch {current_path}` 即可切回。"
            )
            await feishu.send_interactive_reply(chat_id, final_card, reply_to_message_id)
        else:
            progress_card = (
                f"## 🔄 正在切换项目\n\n"
                f"**目标**: `{target_path}`\n\n"
                f"{bar} `{step_obj.step}/{total}` {label}\n\n"
                f"⏳ 切换中，请稍候..."
            )
            await feishu.send_interactive_reply(chat_id, progress_card, reply_to_message_id)


def run_switch_cli(target_path: str, feishu=None, chat_id: str | None = None):
    """CLI version of switch — yields SwitchStep, optionally sends Feishu notifications.

    Args:
        target_path: target project directory
        feishu: FeishuClient instance (optional, for notifications)
        chat_id: Feishu chat_id (optional, required if feishu is provided)
    """
    import asyncio

    async def _run_with_feishu():
        if not feishu or not chat_id:
            # No Feishu — just yield steps without notifications
            for step_obj in switch_to(target_path):
                yield step_obj
            return

        async def _send(card_md: str):
            try:
                await feishu.send_interactive_reply(chat_id, card_md, "")
            except Exception:
                pass  # non-fatal, CLI continues

        # Send initial card
        initial_card = (
            f"## 🔄 正在切换项目\n\n"
            f"**目标**: `{target_path}`\n\n"
            f"⏳ 准备切换，请稍候..."
        )
        await _send(initial_card)

        for step_obj in switch_to(target_path):
            bar = "▓" * step_obj.step + "░" * (5 - step_obj.step)
            label = _FEISHU_STEP_LABELS[step_obj.step - 1] if step_obj.step <= len(_FEISHU_STEP_LABELS) else f"步骤 {step_obj.step}"

            if step_obj.status == "final":
                final_card = (
                    f"## ✅ 切换完成\n\n"
                    f"**目标项目**: `{target_path}`\n"
                    f"**新进程 PID**: `{step_obj.target_pid}`\n\n"
                    f"🎉 飞书消息流已切换到目标项目，继续对话吧！"
                )
                await _send(final_card)
            else:
                progress_card = (
                    f"## 🔄 正在切换项目\n\n"
                    f"**目标**: `{target_path}`\n\n"
                    f"{bar} `{step_obj.step}/5` {label}\n\n"
                    f"⏳ 切换中，请稍候..."
                )
                await _send(progress_card)

            yield step_obj

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        gen = _run_with_feishu()
        # Collect and yield items from the async generator using the same event loop
        try:
            while True:
                yielded = loop.run_until_complete(gen.__anext__())
                yield yielded
        except StopAsyncIteration:
            pass
    finally:
        loop.close()
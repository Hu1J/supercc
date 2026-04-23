"""GatewayManager — 后台常驻服务核心管理类。"""
from __future__ import annotations

import os
import sys
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import supercc.gateway.platform as platform


class GatewayManager:
    """管理 SuperCC Gateway 后台服务。

    Args:
        data_dir: 项目 .supercc/ 目录路径（如 /path/to/project/.supercc）。
                  若不传，则默认取 cwd/.supercc。
    """

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            from supercc.config import resolve_config_path

            _, data_dir = resolve_config_path()
        self._data_dir = data_dir
        os.makedirs(self._data_dir, exist_ok=True)

    @property
    def _pid_file(self) -> str:
        return os.path.join(self._data_dir, "gateway.pid")

    @property
    def _stdout_log(self) -> str:
        return os.path.join(self._data_dir, "gateway-stdout.log")

    @property
    def _stderr_log(self) -> str:
        return os.path.join(self._data_dir, "gateway-stderr.log")

    # ── PID 文件 ──────────────────────────────────────────────────────────────

    def _save_pid(self, pid: int) -> None:
        Path(self._pid_file).write_text(str(pid), encoding="utf-8")

    def _load_pid(self) -> Optional[int]:
        if not os.path.exists(self._pid_file):
            return None
        try:
            return int(Path(self._pid_file).read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def _is_running(self, pid: int) -> bool:
        """检查进程是否存活（发送信号 0）。"""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # ── 服务状态 ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """返回 gateway 状态。"""
        pid = self._load_pid()
        running = pid is not None and self._is_running(pid)
        return {
            "running": running,
            "pid": pid,
            "installed": self._is_installed(),
        }

    def _is_installed(self) -> bool:
        """检查平台服务是否已安装（通过标记文件）。"""
        return Path(self._data_dir).joinpath(".gateway-installed").exists()

    # ── 启动/停止 ────────────────────────────────────────────────────────────

    def start(self, background: bool = True) -> int:
        """启动 gateway 进程。返回 PID。"""
        pid = self._load_pid()
        if pid is not None and self._is_running(pid):
            print(f"Gateway 已在运行（PID {pid}）")
            return pid

        # 后台模式：启动独立会话进程
        if background:
            stdout_f = open(self._stdout_log, "a")
            stderr_f = open(self._stderr_log, "a")
            try:
                proc = subprocess.Popen(
                    ["supercc"],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    start_new_session=True,
                )
                # 等待 PID 文件出现（最多 10 秒）
                for _ in range(50):
                    pid = self._load_pid()
                    if pid is not None and self._is_running(pid):
                        print(f"✅ Gateway 已启动（PID {pid}）")
                        return pid
                    if proc.poll() is not None:
                        raise RuntimeError("Gateway 进程启动后立即退出")
                    time.sleep(0.2)
                # 超时：尝试终止子进程
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise RuntimeError("Gateway 启动超时（PID 文件未出现）")
            finally:
                # 确保文件句柄总是关闭
                stdout_f.close()
                stderr_f.close()
        else:
            # 前台模式：直接启动
            proc = subprocess.Popen(
                [sys.executable, "-m", "supercc", "start"],
            )
            self._save_pid(proc.pid)
            print(f"✅ Gateway 已启动（PID {proc.pid}，前台模式）")
            return proc.pid

    def stop(self) -> None:
        """停止 gateway 进程（SIGTERM → SIGKILL）。"""
        pid = self._load_pid()
        if pid is None:
            print("Gateway 未在运行（无 PID 文件）")
            return

        if not self._is_running(pid):
            print("Gateway 未在运行（进程已死）")
            Path(self._pid_file).unlink(missing_ok=True)
            return

        def kill_with_timeout(pid: int, sig: int, timeout: float) -> bool:
            try:
                os.kill(pid, sig)
            except OSError:
                return True  # 已不存在
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not self._is_running(pid):
                    return True
                time.sleep(0.1)
            return False

        # SIGTERM，5 秒超时
        term_ok = kill_with_timeout(pid, signal.SIGTERM, 5.0)
        if not term_ok:
            # SIGKILL，2 秒超时
            sigkill_ok = kill_with_timeout(pid, signal.SIGKILL, 2.0)
            if not sigkill_ok:
                print(f"⚠️  无法终止进程（PID {pid}），PID 文件仍保留")
                return

        Path(self._pid_file).unlink(missing_ok=True)
        print(f"✅ Gateway（PID {pid}）已停止")

    # ── 服务安装/卸载 ────────────────────────────────────────────────────────

    def _project_slug(self) -> str:
        """从数据目录推导项目 slug（取 .supercc 父目录名，转 DNS 安全格式）。"""
        import re

        parent = Path(self._data_dir).resolve().parent.name
        return re.sub(r"[^a-zA-Z0-9_-]", "_", parent)

    def install(self) -> None:
        """安装平台服务（开机自启动）。"""
        platform.install_service(self._data_dir, self._project_slug())
        # 安装后自动启动
        self.start()

    def uninstall(self) -> None:
        """卸载平台服务。"""
        self.stop()
        platform.uninstall_service(self._data_dir, self._project_slug())

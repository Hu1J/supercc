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
        return os.path.join(self._data_dir, "supercc.pid")

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
        """检查进程是否存活。Windows 用 OpenProcess，避免 kill(pid,0) 的权限问题。"""
        if sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                return False
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
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"

            if sys.platform == "win32":
                # Windows: 使用 pythonw.exe + 正确的进程创建标志
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB
                python_exe = sys.executable
                # 尝试使用 pythonw.exe（无控制台窗口）
                pythonw = str(Path(python_exe).with_name("pythonw.exe"))
                if not Path(pythonw).exists():
                    pythonw = python_exe

                flags = (
                    0x00000008  # DETACHED_PROCESS
                    | 0x00000200  # CREATE_NEW_PROCESS_GROUP
                    | 0x08000000  # CREATE_NO_WINDOW
                    | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
                )
                # 使用项目目录作为工作目录
                project_dir = Path(self._data_dir).resolve().parent
                try:
                    proc = subprocess.Popen(
                        [pythonw, "-m", "supercc", "gateway", "run"],
                        cwd=str(project_dir),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        creationflags=flags,
                        env=env,
                        close_fds=True,
                    )
                except OSError:
                    # pythonw.exe 不可用，回退到 python.exe
                    flags = flags & ~0x08000000  # 去掉 CREATE_NO_WINDOW
                    proc = subprocess.Popen(
                        [python_exe, "-m", "supercc", "gateway", "run"],
                        cwd=str(project_dir),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        creationflags=flags,
                        env=env,
                        close_fds=True,
                    )
            else:
                # macOS/Linux: 使用 start_new_session
                proc = subprocess.Popen(
                    [sys.executable, "-m", "supercc", "gateway", "run"],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    start_new_session=True,
                    env=env,
                )

            # 等待 PID 文件出现（最多 10 秒）
            for _ in range(50):
                pid = self._load_pid()
                if pid is not None and self._is_running(pid):
                    print(f"✅ Gateway 已启动（PID {pid}）")
                    stdout_f.close()
                    stderr_f.close()
                    return pid
                if proc.poll() is not None:
                    stdout_f.close()
                    stderr_f.close()
                    raise RuntimeError("Gateway 进程启动后立即退出")
                time.sleep(0.2)
            # 超时：尝试终止子进程
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stdout_f.close()
            stderr_f.close()
            raise RuntimeError("Gateway 启动超时（PID 文件未出现）")
        else:
            # 前台模式：直接启动
            proc = subprocess.Popen(
                [sys.executable, "-m", "supercc", "start"],
            )
            self._save_pid(proc.pid)
            print(f"✅ Gateway 已启动（PID {proc.pid}，前台模式）")
            return proc.pid

    def _launchd_plist_path(self) -> Path:
        """返回 launchd plist 文件路径（与 install_mac 保持一致）。"""
        slug = self._project_slug()
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        return plist_dir / f"com.supercc.gateway.{slug}.plist"

    def stop(self) -> None:
        """通过平台服务停止 gateway（仅 stop，不卸载 plist）。"""
        platform.stop_service(self._data_dir, self._project_slug())
        Path(self._pid_file).unlink(missing_ok=True)

    # ── 服务安装/卸载 ────────────────────────────────────────────────────────

    def _project_slug(self) -> str:
        """从数据目录推导项目 slug（纯路径 hash，保证同名项目不冲突）。"""
        import hashlib

        path = Path(self._data_dir).resolve().parent
        return hashlib.md5(str(path).encode()).hexdigest()[:8]

    def install(self) -> None:
        """安装平台服务（开机自启动）。

        install_service 会写入 plist 并执行 launchctl bootstrap/systemctl enable，
        服务会立即启动并加入开机自启。
        """
        platform.install_service(self._data_dir, self._project_slug())

    def uninstall(self) -> None:
        """卸载平台服务。"""
        self.stop()
        platform.uninstall_service(self._data_dir, self._project_slug())

"""Gateway CLI 处理器 — supercc gateway start/stop/status/restart"""
from __future__ import annotations

from supercc.gateway.manager import GatewayManager


def _gm() -> GatewayManager:
    """构造 GatewayManager，使用当前项目的 .supercc/ 目录。"""
    from supercc.config import resolve_config_path

    _, data_dir = resolve_config_path()
    return GatewayManager(data_dir)


def run_gateway_start(force: bool = False) -> None:
    """gateway start 子命令：启动 gateway（未安装则自动安装）。

    Args:
        force: 如果 True，强制重新安装服务（刷新 Token 等）
    """
    import sys
    gm = _gm()
    status = gm.status()
    if not status["installed"] or force:
        if force:
            print("强制重新安装 Gateway 服务...")
            gm.uninstall()
        else:
            print("Gateway 未安装，正在安装...")
        gm.install()

    # 通过平台服务管理器启动（macOS → launchctl kickstart, Linux → systemctl start）
    if sys.platform == "darwin":
        from supercc.gateway.platform import kickstart_mac
        kickstart_mac(gm._data_dir, gm._project_slug())
    elif sys.platform.startswith("linux"):
        from supercc.gateway.platform import kickstart_linux
        kickstart_linux(gm._data_dir, gm._project_slug())
    else:
        # Windows: 弹出新 cmd 窗口执行 gateway run
        if not status.get("running"):
            import subprocess
            from pathlib import Path
            project_dir = str(Path(gm._data_dir).resolve().parent)
            # Windows console script 位于 Scripts 目录
            scripts_dir = Path(sys.executable).parent.parent / "Scripts"
            supercc_exe = scripts_dir / "supercc.exe"
            if not supercc_exe.exists():
                supercc_exe = scripts_dir / "supercc.cmd"
            if not supercc_exe.exists():
                supercc_exe = scripts_dir / "supercc.bat"
            if supercc_exe.exists():
                supercc_path = str(supercc_exe)
            else:
                supercc_path = "supercc"  # 兜底
            # 使用 cmd /c start 避免 shell 解析问题
            cmd = f'start "SuperCC Gateway" cmd /c ""{supercc_path}" gateway run --working-dir "{project_dir}" & pause"'
            subprocess.Popen(cmd, shell=True)
            print("✅ Gateway 已启动（新窗口运行）")


def run_gateway_stop() -> None:
    """gateway stop 子命令：停止 gateway。"""
    gm = _gm()
    gm.stop()


def run_gateway_status() -> None:
    """gateway status 子命令：查看运行状态。"""
    gm = _gm()
    s = gm.status()
    if s["running"]:
        print(f"🟢 Gateway 运行中（PID {s['pid']}）")
    else:
        print("⚪ Gateway 未运行")
    if s["installed"]:
        print("✅ 平台服务已安装（开机自启动）")
    elif s["running"]:
        print("⚡ 前台运行中（非服务模式，适合开发调试）")
    else:
        print("❌ 平台服务未安装（不会开机自启动）")


def run_gateway_run() -> None:
    """gateway run 子命令：前台阻塞运行，不获取 filelock。
    适合开发调试，Ctrl+C 退出。
    """
    import os
    from supercc.config import resolve_config_path, init_config
    cfg_path, data_dir = resolve_config_path()
    init_config(cfg_path)

    # 保存 PID 文件，让 status 命令能看到运行状态
    pid_file = os.path.join(data_dir, "supercc.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    import atexit
    atexit.register(lambda: os.path.exists(pid_file) and os.unlink(pid_file))

    # 在 start_bridge 之前设置文件日志（覆盖所有模块，包括 supercc）
    import logging
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        from supercc.main import PlainFormatter
        fh = logging.FileHandler(log_file, mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
    except Exception:
        pass

    import asyncio
    from supercc.main import start_bridge
    asyncio.run(start_bridge(cfg_path, data_dir, foreground=True))


def run_gateway_restart() -> None:
    """gateway restart 子命令：热重启当前实例。"""
    import sys
    from supercc.core.commands.restart_impl import run_restart_cli
    try:
        for step in run_restart_cli(None):
            print(f"[{step.step}/{step.total}] {step.label}: {step.detail or ''}")
            if step.status == "final":
                break
    except Exception as e:
        print(f"Restart failed: {e}", file=sys.stderr)
        sys.exit(1)

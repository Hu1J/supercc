"""跨平台服务安装 — macOS (launchd) / Linux (systemd) / Windows (Task Scheduler)。"""
from __future__ import annotations

import os
import signal
import sys
import time
import subprocess
from pathlib import Path


class ServiceType:
    MAIN = "main"


def get_platform() -> str:
    """返回当前平台: 'macos' | 'linux' | 'windows'"""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform == "win32":
        return "windows"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _ensure_user_systemd_env() -> None:
    """确保 DBUS_SESSION_BUS_ADDRESS 和 XDG_RUNTIME_DIR 已设置。

    在 SSH 无桌面 session 环境下，这些环境变量可能缺失。
    直接调用 systemctl --user 会报错 "Failed to connect to bus"。
    此函数检测 socket 路径并补全缺失的环境变量。
    """
    uid = os.getuid()
    if "XDG_RUNTIME_DIR" not in os.environ:
        runtime_dir = f"/run/user/{uid}"
        if Path(runtime_dir).exists():
            os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        bus_path = Path(xdg_runtime) / "bus"
        if bus_path.exists():
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def _resolve_supercc() -> str:
    """返回当前环境 supercc console script 绝对路径。"""
    python_path = Path(sys.executable)
    return str(python_path.parent / "supercc")


def _get_start_script(data_dir: str) -> str:
    """生成 bridge 启动脚本内容。

    所有平台统一使用 --working-dir 参数指定项目目录，不再依赖 cd。
    """
    project_dir = Path(data_dir).resolve().parent
    supercc_path = _resolve_supercc()
    return (
        f"#!/bin/bash\n"
        f"exec {supercc_path} gateway run --working-dir {project_dir}\n"
    )


def _slug_to_dns_safe(slug: str) -> str:
    """将 slug 转换为 DNS 安全格式（只含字母、数字、连字符、下划线）。"""
    import re

    return re.sub(r"[^a-zA-Z0-9_-]", "_", slug)


# ── macOS: launchd plist ──────────────────────────────────────────────────────

def install_mac(data_dir: str, project_slug: str) -> None:
    """安装 macOS LaunchAgent。

    直接写 ProgramArguments 运行 supercc gateway run，不使用 wrapper 脚本。
    """
    slug = _slug_to_dns_safe(project_slug)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)

    plist_name = f"com.supercc.main.{slug}"
    plist_path = plist_dir / f"{plist_name}.plist"

    project_dir = Path(data_dir).resolve().parent
    supercc_path = _resolve_supercc()

    # 构建 launchd 可识别的 PATH（launchd 默认只有 /usr/bin:/bin:/usr/sbin:/sbin）
    # 捕获当前环境的 PATH 和 VIRTUAL_ENV，确保 conda 环境下的 supercc 可执行
    sane_path = os.environ.get("PATH", "/usr/bin:/bin")
    venv_dir = os.environ.get("VIRTUAL_ENV", "")

    # 写入 plist
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{plist_name}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{supercc_path}</string>
        <string>gateway</string>
        <string>run</string>
        <string>--working-dir</string>
        <string>{project_dir}</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{sane_path}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>{Path(data_dir) / "gateway-stdout.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path(data_dir) / "gateway-stderr.log"}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")

    # 标记文件
    Path(data_dir).joinpath(".gateway-installed").touch()

    # bootout 旧服务（如已加载），再 bootstrap（如已加载则跳过）
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{plist_name}"],
        capture_output=True, text=True, timeout=30,
    )
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True, timeout=30,
    )
    print(f"✅ Gateway 已安装到 macOS LaunchAgent: {plist_path}")


def uninstall_mac(data_dir: str, project_slug: str) -> None:
    """卸载 macOS LaunchAgent（bootout + 删除 plist）。"""
    slug = _slug_to_dns_safe(project_slug)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_name = f"com.supercc.main.{slug}"
    plist_path = plist_dir / f"{plist_name}.plist"

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{plist_name}"],
        capture_output=True, check=False, timeout=90,
    )
    plist_path.unlink(missing_ok=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)
    print("✅ Gateway 已从 macOS LaunchAgent 卸载")


def stop_mac(data_dir: str, project_slug: str) -> None:
    """停止 macOS LaunchAgent 服务：先删除服务，再杀实例。"""
    slug = _slug_to_dns_safe(project_slug)
    plist_name = f"com.supercc.main.{slug}"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{plist_name}.plist"

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{plist_name}"],
        check=False, timeout=90,
    )
    # 删除 plist 和标记文件（服务删除）
    plist_path.unlink(missing_ok=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)

    # 杀实例
    pid_file = Path(data_dir) / "supercc.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid:
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    lock_file = Path(data_dir) / ".instance.lock"
    lock_file.unlink(missing_ok=True)

    print("✅ Gateway 已停止")


# ── Linux: systemd user service ───────────────────────────────────────────────

def install_linux(data_dir: str, project_slug: str) -> None:
    """安装 systemd user service。"""
    _ensure_user_systemd_env()
    slug = _slug_to_dns_safe(project_slug)
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    service_name = f"supercc-main-{slug}"
    service_path = service_dir / f"{service_name}.service"
    script_path = service_dir / f"{service_name}.sh"

    # 写入启动脚本
    script_path.write_text(_get_start_script(data_dir), encoding="utf-8")
    os.chmod(script_path, 0o755)

    # 写入 service 文件
    service_content = f"""[Unit]
Description=SuperCC Main ({slug})

[Service]
ExecStart={script_path}
Restart=unless-stopped
RestartSec=5
StandardOutput=append:{Path(data_dir) / "gateway-stdout.log"}
StandardError=append:{Path(data_dir) / "gateway-stderr.log"}

[Install]
WantedBy=default.target
"""
    service_path.write_text(service_content, encoding="utf-8")

    # 标记文件
    Path(data_dir).joinpath(".gateway-installed").touch()

    # daemon-reload + enable + start
    r1 = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    r2 = subprocess.run(["systemctl", "--user", "enable", service_name], capture_output=True, text=True)
    if r2.returncode != 0:
        print(f"⚠️  systemctl --user enable 失败: {r2.stderr.strip() or r2.stdout.strip()}")
        print("   可能是用户 session 未激活（systemd --user 需要 active session）")
    else:
        # 立即启动服务
        r3 = subprocess.run(["systemctl", "--user", "start", service_name], capture_output=True, text=True)
        if r3.returncode != 0:
            print(f"⚠️  systemctl --user start 失败: {r3.stderr.strip()}")
        else:
            print(f"✅ Gateway 已安装并启动为 systemd user service: {service_path}")


def uninstall_linux(data_dir: str, project_slug: str) -> None:
    """卸载 systemd user service（disable + 删除文件）。"""
    _ensure_user_systemd_env()
    slug = _slug_to_dns_safe(project_slug)
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_name = f"supercc-main-{slug}"
    service_path = service_dir / f"{service_name}.service"
    script_path = service_dir / f"{service_name}.sh"

    subprocess.run(["systemctl", "--user", "disable", service_name], capture_output=True, text=True)
    service_path.unlink(missing_ok=True)
    script_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)
    print("✅ Gateway 已从 systemd user service 卸载")


def stop_linux(data_dir: str, project_slug: str) -> None:
    """停止 systemd user service：先删除服务，再杀实例。"""
    _ensure_user_systemd_env()
    slug = _slug_to_dns_safe(project_slug)
    service_name = f"supercc-main-{slug}"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_path = service_dir / f"{service_name}.service"
    script_path = service_dir / f"{service_name}.sh"

    # 删除服务（disable + 删除文件）
    subprocess.run(["systemctl", "--user", "disable", service_name], capture_output=True, text=True)
    service_path.unlink(missing_ok=True)
    script_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)

    # 读取 PID（用于杀实例）
    pid_file = Path(data_dir) / "supercc.pid"
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pass

    result = subprocess.run(["systemctl", "--user", "stop", service_name], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Could not find" not in stderr:
            print(f"⚠️  systemctl --user stop 失败: {stderr}")

    # 等待进程真正退出（最多 5 秒）
    if pid:
        for _ in range(50):  # 50 * 0.1s = 5s
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    # 删除 .instance.lock
    lock_file = Path(data_dir) / ".instance.lock"
    lock_file.unlink(missing_ok=True)

    print("✅ Gateway 已停止")


# ── Windows: Task Scheduler ─────────────────────────────────────────────────────

def install_windows(data_dir: str, project_slug: str) -> None:
    """安装 Windows Task Scheduler 任务。"""
    slug = _slug_to_dns_safe(project_slug)
    task_name = f"SuperCC Main ({slug})"
    script_path = Path.home() / ".supercc" / f"supercc-main-{slug}.bat"
    project_dir = Path(data_dir).resolve().parent
    supercc_path = _resolve_supercc()
    # 脚本直接调用 gateway run，由 manager.py 的 _spawn_detached 处理进程创建
    script_content = (
        f'@echo off\n'
        f'cd /d "{project_dir}"\n'
        f'"{supercc_path}" gateway run --working-dir "{project_dir}"\n'
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_content, encoding="utf-8")

    # 创建任务（At logon + At startup）
    cmds = [
        [
            "schtasks", "/create", "/tn", task_name,
            "/tr", f'"{script_path}"',
            "/sc", "onlogon",
            "/rl", "limited",
            "/f",
        ],
        [
            "schtasks", "/create", "/tn", f"{task_name} (Startup)",
            "/tr", f'"{script_path}"',
            "/sc", "onstart",
            "/rl", "limited",
            "/f",
        ],
    ]

    # 标记文件
    Path(data_dir).joinpath(".gateway-installed").touch()

    failed = []
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(r.stderr.strip() or r.stdout.strip())
    if failed:
        print(f"⚠️  部分 Task Scheduler 任务创建失败: {failed}")
    else:
        print(f"✅ Gateway 已安装为 Windows Task Scheduler 任务: {task_name}")


def stop_windows(data_dir: str, project_slug: str) -> None:
    """删除系统服务并停止运行中的 Gateway 进程。"""
    # 先删除系统服务（Task Scheduler 任务）
    uninstall_windows(data_dir, project_slug)
    # 再停止 Gateway 进程
    from supercc.gateway.manager import GatewayManager
    gm = GatewayManager(data_dir)
    pid = gm._load_pid()
    if pid is None:
        print("Gateway 未运行")
        return
    try:
        import os
        os.kill(pid, 9)  # SIGKILL
        print(f"✅ Gateway 已停止（PID {pid}）")
    except ProcessLookupError:
        print("Gateway 未运行")
    except PermissionError:
        print(f"⚠️  无权限终止 PID {pid}，请使用管理员模式")
    # 清理 PID 文件
    Path(gm._pid_file).unlink(missing_ok=True)


def uninstall_windows(data_dir: str, project_slug: str) -> None:
    """卸载 Windows Task Scheduler 任务。"""
    slug = _slug_to_dns_safe(project_slug)
    task_name = f"SuperCC Main ({slug})"
    failed = []
    for variant in [task_name, f"{task_name} (Startup)"]:
        r = subprocess.run(
            ["schtasks", "/delete", "/tn", variant, "/f"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            failed.append(variant)
    script_path = Path.home() / ".supercc" / f"supercc-main-{slug}.bat"
    script_path.unlink(missing_ok=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)
    if failed:
        print(f"⚠️  以下任务删除失败: {failed}，但脚本文件已删除")
    else:
        print("✅ Gateway 已从 Windows Task Scheduler 卸载")


# ── 统一入口 ─────────────────────────────────────────────────────────────────

def install_service(data_dir: str, project_slug: str) -> None:
    """根据当前平台安装 gateway 服务。"""
    p = get_platform()
    if p == "macos":
        install_mac(data_dir, project_slug)
    elif p == "linux":
        install_linux(data_dir, project_slug)
    elif p == "windows":
        install_windows(data_dir, project_slug)
    else:
        raise RuntimeError(f"Unsupported platform: {p}")


def stop_service(data_dir: str, project_slug: str) -> None:
    """根据当前平台停止 gateway 服务（仅 stop，不删除 plist/脚本）。"""
    p = get_platform()
    if p == "macos":
        stop_mac(data_dir, project_slug)
    elif p == "linux":
        stop_linux(data_dir, project_slug)
    elif p == "windows":
        stop_windows(data_dir, project_slug)
    else:
        raise RuntimeError(f"Unsupported platform: {p}")


def uninstall_service(data_dir: str, project_slug: str) -> None:
    """根据当前平台卸载 gateway 服务。"""
    p = get_platform()
    if p == "macos":
        uninstall_mac(data_dir, project_slug)
    elif p == "linux":
        uninstall_linux(data_dir, project_slug)
    elif p == "windows":
        uninstall_windows(data_dir, project_slug)
    else:
        raise RuntimeError(f"Unsupported platform: {p}")


def kickstart_mac(data_dir: str, project_slug: str) -> None:
    """通过 launchctl 启动已安装的 LaunchAgent 服务（kickstart）。

    如服务未加载则自动重新 bootstrap（参考 Hermes 实现：先 kickstart，
    失败 error code 3/113 时重新 bootstrap）。plist 缺失时自动重建。
    """
    slug = _slug_to_dns_safe(project_slug)
    plist_name = f"com.supercc.main.{slug}"
    uid = os.getuid()
    target = f"gui/{uid}/{plist_name}"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{plist_name}.plist"
    try:
        subprocess.run(
            ["launchctl", "kickstart", target],
            check=True, timeout=30,
        )
        print("✅ Gateway 已通过 launchd 启动")
    except subprocess.CalledProcessError as e:
        if e.returncode not in {3, 113}:
            print(f"⚠️  launchctl kickstart 失败 (code {e.returncode})")
            raise
        # plist 缺失时自动重建
        if not plist_path.exists():
            print("↻ launchd plist 缺失，正在重新安装...")
            install_mac(data_dir, project_slug)
            return
        # 服务未加载 → 重新 bootstrap 后再 kickstart
        print("↻ launchd 服务未加载，正在重新注册...")
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            check=True, timeout=30,
        )
        subprocess.run(
            ["launchctl", "kickstart", target],
            check=True, timeout=30,
        )
        print("✅ Gateway 已通过 launchd 启动")


def kickstart_linux(data_dir: str, project_slug: str) -> None:
    """通过 systemctl 启动已安装的 systemd user service。"""
    _ensure_user_systemd_env()
    slug = _slug_to_dns_safe(project_slug)
    service_name = f"supercc-main-{slug}"
    result = subprocess.run(
        ["systemctl", "--user", "start", service_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"⚠️  systemctl --user start 失败: {result.stderr.strip()}")


def _is_service_installed(data_dir: str) -> bool:
    """检查平台服务是否已安装（通过标记文件）。"""
    return Path(data_dir).joinpath(".gateway-installed").exists()

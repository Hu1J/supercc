"""跨平台服务安装 — macOS (launchd) / Linux (systemd) / Windows (Task Scheduler)。"""
from __future__ import annotations

import os
import sys
import subprocess
import shutil
from pathlib import Path


def get_platform() -> str:
    """返回当前平台: 'macos' | 'linux' | 'windows'"""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform == "win32":
        return "windows"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def _resolve_supercc() -> str:
    """返回当前环境 supercc console script 绝对路径。"""
    python_path = Path(sys.executable)
    return str(python_path.parent / "supercc")


def _get_start_script(data_dir: str) -> str:
    """生成启动脚本内容。"""
    project_dir = Path(data_dir).resolve().parent
    return f"#!/bin/bash\ncd {project_dir}\nexec {_resolve_supercc()} start\n"


def _slug_to_dns_safe(slug: str) -> str:
    """将 slug 转换为 DNS 安全格式（只含字母、数字、连字符、下划线）。"""
    import re

    return re.sub(r"[^a-zA-Z0-9_-]", "_", slug)


# ── macOS: launchd plist ──────────────────────────────────────────────────────

def install_mac(data_dir: str, project_slug: str) -> None:
    """安装 macOS LaunchAgent。"""
    slug = _slug_to_dns_safe(project_slug)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)

    plist_name = f"com.supercc.gateway.{slug}"
    script_name = f"com.supercc.gateway.{slug}.sh"
    plist_path = plist_dir / f"{plist_name}.plist"
    script_path = plist_dir / script_name

    # 写入启动脚本
    script_path.write_text(_get_start_script(data_dir), encoding="utf-8")
    os.chmod(script_path, 0o755)

    # 写入 plist
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{plist_name}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
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

    # 加载服务
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  launchctl load 失败: {result.stderr.strip() or result.stdout.strip()}")
    else:
        print(f"✅ Gateway 已安装到 macOS LaunchAgent: {plist_path}")


def uninstall_mac(data_dir: str, project_slug: str) -> None:
    """卸载 macOS LaunchAgent（unload + 删除 plist + 删除脚本）。"""
    slug = _slug_to_dns_safe(project_slug)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_name = f"com.supercc.gateway.{slug}"
    plist_path = plist_dir / f"{plist_name}.plist"
    script_path = plist_dir / f"{plist_name}.sh"

    result = subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  launchctl unload 失败（可能服务未加载），文件未删除")
        return
    plist_path.unlink(missing_ok=True)
    script_path.unlink(missing_ok=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)
    print("✅ Gateway 已从 macOS LaunchAgent 卸载")


def stop_mac(data_dir: str, project_slug: str) -> None:
    """停止 macOS LaunchAgent 服务（仅 unload，不删除 plist）。"""
    slug = _slug_to_dns_safe(project_slug)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"com.supercc.gateway.{slug}.plist"

    result = subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Not loaded" not in stderr and "No such file" not in stderr:
            print(f"⚠️  launchctl unload 失败: {stderr}")
            return
    print("✅ Gateway 已停止")


# ── Linux: systemd user service ───────────────────────────────────────────────

def install_linux(data_dir: str, project_slug: str) -> None:
    """安装 systemd user service。"""
    slug = _slug_to_dns_safe(project_slug)
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    service_name = f"supercc-gateway-{slug}"
    service_path = service_dir / f"{service_name}.service"
    script_path = service_dir / f"{service_name}.sh"

    # 写入启动脚本
    script_path.write_text(_get_start_script(data_dir), encoding="utf-8")
    os.chmod(script_path, 0o755)

    # 写入 service 文件
    service_content = f"""[Unit]
Description=SuperCC Gateway ({slug})

[Service]
ExecStart={script_path}
Restart=always
RestartSec=5
StandardOutput=append:{Path(data_dir) / "gateway-stdout.log"}
StandardError=append:{Path(data_dir) / "gateway-stderr.log"}

[Install]
WantedBy=default.target
"""
    service_path.write_text(service_content, encoding="utf-8")

    # 标记文件
    Path(data_dir).joinpath(".gateway-installed").touch()

    # daemon-reload + enable
    r1 = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    r2 = subprocess.run(["systemctl", "--user", "enable", service_name], capture_output=True, text=True)
    if r2.returncode != 0:
        print(f"⚠️  systemctl --user enable 失败: {r2.stderr.strip() or r2.stdout.strip()}")
        print("   可能是用户 session 未激活（systemd --user 需要 active session）")
    else:
        print(f"✅ Gateway 已安装为 systemd user service: {service_path}")


def uninstall_linux(data_dir: str, project_slug: str) -> None:
    """卸载 systemd user service（disable + 删除文件）。"""
    slug = _slug_to_dns_safe(project_slug)
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_name = f"supercc-gateway-{slug}"
    service_path = service_dir / f"{service_name}.service"
    script_path = service_dir / f"{service_name}.sh"

    subprocess.run(["systemctl", "--user", "disable", service_name], capture_output=True, text=True)
    service_path.unlink(missing_ok=True)
    script_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    Path(data_dir).joinpath(".gateway-installed").unlink(missing_ok=True)
    print("✅ Gateway 已从 systemd user service 卸载")


def stop_linux(data_dir: str, project_slug: str) -> None:
    """停止 systemd user service（仅 stop，不 disable）。"""
    slug = _slug_to_dns_safe(project_slug)
    service_name = f"supercc-gateway-{slug}"
    result = subprocess.run(["systemctl", "--user", "stop", service_name], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Could not find" not in stderr:
            print(f"⚠️  systemctl --user stop 失败: {stderr}")
            return
    print("✅ Gateway 已停止")


# ── Windows: Task Scheduler ─────────────────────────────────────────────────────

def install_windows(data_dir: str, project_slug: str) -> None:
    """安装 Windows Task Scheduler 任务。"""
    slug = _slug_to_dns_safe(project_slug)
    task_name = f"SuperCC Gateway ({slug})"
    script_path = Path.home() / ".supercc" / f"supercc-gateway-{slug}.bat"
    project_dir = Path(data_dir).resolve().parent
    script_content = f'@echo off\ncd /d "{project_dir}"\nsupercc start\n'
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
    """Windows Task Scheduler 任务没有"停止"概念（只在触发时运行）。"""
    print("⚠️  Windows 不支持 stop（Task Scheduler 任务非持久运行），请使用 uninstall")


def uninstall_windows(data_dir: str, project_slug: str) -> None:
    """卸载 Windows Task Scheduler 任务。"""
    slug = _slug_to_dns_safe(project_slug)
    task_name = f"SuperCC Gateway ({slug})"
    failed = []
    for variant in [task_name, f"{task_name} (Startup)"]:
        r = subprocess.run(
            ["schtasks", "/delete", "/tn", variant, "/f"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            failed.append(variant)
    script_path = Path.home() / ".supercc" / f"supercc-gateway-{slug}.bat"
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

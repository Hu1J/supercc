"""Gateway CLI 处理器 — supercc gateway install/start/stop/status"""
from __future__ import annotations

from supercc.gateway.manager import GatewayManager


def _gm() -> GatewayManager:
    """构造 GatewayManager，使用当前项目的 .supercc/ 目录。"""
    from supercc.config import resolve_config_path

    _, data_dir = resolve_config_path()
    return GatewayManager(data_dir)


def run_gateway_install() -> None:
    """gateway install 子命令：安装平台服务（开机自启动）。"""
    gm = _gm()
    if gm.status()["installed"]:
        print("Gateway 服务已安装，如需重新安装请先卸载：supercc gateway uninstall")
        return
    gm.install()


def run_gateway_start() -> None:
    """gateway start 子命令：启动 gateway（未安装则自动安装）。"""
    gm = _gm()
    status = gm.status()
    if not status["installed"]:
        print("Gateway 未安装，正在安装...")
        gm.install()
    else:
        gm.start()


def run_gateway_stop() -> None:
    """gateway stop 子命令：停止 gateway。"""
    gm = _gm()
    gm.stop()


def run_gateway_uninstall() -> None:
    """gateway uninstall 子命令：卸载平台服务并停止运行。"""
    gm = _gm()
    if not gm.status()["installed"]:
        print("Gateway 未安装，无需卸载")
        return
    gm.uninstall()


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
    else:
        print("❌ 平台服务未安装（不会开机自启动）")

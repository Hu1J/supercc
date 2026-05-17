"""WhatsApp 插件独立进程入口。

Usage:
    python -m supercc.channels.whatsapp
    # 环境变量：
    #   SUPERCC_CONFIG=项目路径/.supercc/config.json
    #   SUPERCC_DATA=项目路径/.supercc/
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 将项目根目录加入 sys.path（确保能 import supercc）
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from supercc.config import init_config, get_config
from supercc.channels.whatsapp.bridge_client import WhatsAppBridgeClient
from supercc.channels.whatsapp.core_client import WhatsAppCoreWSClient, run_bridge_polling
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("whatsapp")

# Bridge 目录（相对于项目根目录）
BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "whatsapp-bridge"
BRIDGE_PORT_DEFAULT = 3000


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so whatsapp plugin logs also go to supercc.log."""
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        fh = logging.FileHandler(log_file, mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
        logger.debug("File logging added: %s", log_file)
    except Exception as e:
        logger.warning("Failed to add file logging: %s", e)


def _check_node() -> bool:
    """检查 Node.js 是否可用。"""
    return shutil.which("node") is not None


def _start_bridge(bridge_dir: Path, session_dir: Path, port: int) -> subprocess.Popen:
    """
    启动 Node.js bridge 进程。

    Args:
        bridge_dir: bridge.js 所在目录
        session_dir: WhatsApp session 存储目录
        port: HTTP 服务端口

    Returns:
        subprocess.Popen 对象

    Raises:
        RuntimeError: Node.js 不可用或 npm install 失败
    """
    if not _check_node():
        raise RuntimeError(
            "WhatsApp bridge requires Node.js but 'node' command not found. "
            "Please install Node.js from https://nodejs.org/"
        )

    # 确保 session 目录存在
    session_dir.mkdir(parents=True, exist_ok=True)

    # 安装依赖（如需要）
    node_modules = bridge_dir / "node_modules"
    if not node_modules.exists():
        logger.info("Installing npm dependencies for WhatsApp bridge...")
        result = subprocess.run(
            ["npm", "install"],
            cwd=str(bridge_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"npm install failed: {result.stderr}")
        logger.info("npm dependencies installed")

    # 启动 bridge 进程
    logger.info(
        "Starting WhatsApp bridge on port %s, session dir: %s",
        port,
        session_dir,
    )
    proc = subprocess.Popen(
        [
            "node",
            str(bridge_dir / "bridge.js"),
            "--port", str(port),
            "--session", str(session_dir),
        ],
        cwd=str(bridge_dir),
        # 继承标准输入用于 QR 码显示
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc


async def _wait_for_bridge(bridge: WhatsAppBridgeClient, timeout: float = 30.0) -> bool:
    """
    等待 bridge 就绪（健康检查）。

    Returns:
        True if bridge is ready, False if timeout.
    """
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        try:
            health = await bridge.health_check()
            if health.get("status") == "connected":
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def run_plugin(config, data_dir):
    """WhatsApp 插件协程：在同进程 event loop 中运行。"""
    _setup_file_logging(data_dir)

    # 读取配置
    cfg = config.channels.whatsapp
    bridge_port = cfg.bridge_port or BRIDGE_PORT_DEFAULT
    session_dir = Path(cfg.session_dir) if cfg.session_dir else Path.home() / ".supercc" / "whatsapp-session"

    if not cfg.enabled:
        logger.error("WhatsApp plugin is not enabled in config")
        return

    # 1. 启动 Node.js bridge 进程
    bridge_proc = _start_bridge(BRIDGE_DIR, session_dir, bridge_port)
    logger.info("WhatsApp bridge process started (pid=%s)", bridge_proc.pid)

    # 2. 等待 bridge 就绪
    bridge_client = WhatsAppBridgeClient(port=bridge_port)
    ready = await _wait_for_bridge(bridge_client)
    if not ready:
        logger.error("WhatsApp bridge failed to start within timeout")
        bridge_proc.terminate()
        return

    logger.info("WhatsApp bridge is ready")

    # 3. 创建 Thin Client（连接 Core）
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info("Connecting to core at %s", core_url)

    core_client = WhatsAppCoreWSClient(
        core_url=core_url,
        bridge_port=bridge_port,
        allowed_users=cfg.allowed_users or [],
        project_path=config.claude.approved_directory,
    )

    # 4. 连接到 Core
    try:
        await core_client.connect()
        logger.info("Connected to core")
    except Exception as e:
        logger.error("Failed to connect to core: %s", e)
        bridge_proc.terminate()
        return

    # 5. 启动 bridge 长轮询（接收 WhatsApp 消息）
    polling_task = asyncio.create_task(
        run_bridge_polling(bridge_client, core_client.send_message)
    )
    logger.info("Bridge polling started")

    # 6. 保持进程运行，直到被取消
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        # 清理
        logger.info("Shutting down WhatsApp plugin...")
        polling_task.cancel()
        await core_client.close()
        bridge_proc.terminate()
        logger.info("WhatsApp plugin shutdown complete")


async def main():
    """独立进程入口。"""
    config_path = os.environ.get("SUPERCC_CONFIG", "")
    data_dir = os.environ.get("SUPERCC_DATA", "")

    if not config_path:
        raise RuntimeError("SUPERCC_CONFIG environment variable is required")
    if not data_dir:
        raise RuntimeError("SUPERCC_DATA environment variable is required")

    config = init_config(config_path, data_dir)
    await run_plugin(config, data_dir)


if __name__ == "__main__":
    asyncio.run(main())

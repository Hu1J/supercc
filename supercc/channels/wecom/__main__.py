"""WeCom 插件独立进程入口。

Usage:
    python -m supercc.channels.wecom
    # 环境变量：
    #   SUPERCC_CONFIG=项目路径/.supercc/config.json
    #   SUPERCC_DATA=项目路径/.supercc/
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path（确保能 import supercc）
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from supercc.config import init_config, get_config
from supercc.channels.wecom.client import WeComClient
from supercc.channels.wecom.ws_client import WeComWSClient
from supercc.channels.wecom.core_client import WeComCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("wecom")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so wecom plugin logs also go to supercc.log."""
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        fh = logging.FileHandler(log_file, mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
        logger.debug("File logging added: %s", log_file)
    except Exception as e:
        logger.warning("Failed to add file logging: %s", e)


async def run_plugin(config, data_dir):
    """WeCom 插件协程：在同进程 event loop 中运行。"""
    _setup_file_logging(data_dir)

    # WebSocket 凭证：优先使用扫码接入获得的 bot_id/secret，
    # 回退到手动输入时的 agent_id/corp_secret（向后兼容）
    ws_bot_id = config.channels.wecom.bot_id or config.channels.wecom.agent_id
    ws_bot_secret = config.channels.wecom.secret or config.channels.wecom.corp_secret

    if not ws_bot_id or not ws_bot_secret:
        raise RuntimeError("WeCom bot_id and secret are required (configure via QR scan or manual input)")

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"Connecting to core at {core_url}")

    # 1. 创建 aiohttp WebSocket 客户端（接收 WeCom 消息）
    ws_client = WeComWSClient(
        bot_id=ws_bot_id,
        bot_secret=ws_bot_secret,
        on_message=None,
    )

    # 2. 创建消息发送客户端
    wecom = WeComClient(ws_client)

    # 3. 创建 Thin Client（连接 Core）
    core_client = WeComCoreWSClient(
        core_url=core_url,
        ws_client=ws_client,
        wecom_client=wecom,
        bot_id=ws_bot_id,
        project_path=config.claude.approved_directory,
        data_dir=data_dir,
        groups=config.channels.wecom.groups,
        allowed_users=config.channels.wecom.allowed_users,
    )

    # ws_client 的消息回调指向 core_client.send_message
    ws_client._on_message = core_client.send_message

    # 连接到 Core
    try:
        await core_client.connect()
        logger.info("Connected to core")

        # 启动 WeCom WS 客户端（非阻塞，asyncio.to_thread 避免阻塞主 loop）
        await asyncio.to_thread(ws_client.start)
        logger.info("WeCom WS client started")

        # 保持进程运行，直到被取消
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("WeCom plugin task cancelled, shutting down...")
            raise
    finally:
        # 确保退出时关闭 ws_client，停止 daemon 线程
        logger.info("Shutting down WeCom client...")
        try:
            # 给close一个超时，避免阻塞整个shutdown
            await asyncio.wait_for(ws_client.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("WeCom client close timeout, forcing...")
        except Exception as e:
            logger.warning("Error closing WeCom client: %s", e)
        logger.info("WeCom client shutdown complete")


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
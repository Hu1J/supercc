"""Telegram 插件独立进程入口。

Usage:
    python -m supercc.channels.telegram
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

from supercc.config import init_config
from supercc.channels.telegram.client import TelegramClient
from supercc.channels.telegram.ws_client import TelegramWSClient
from supercc.channels.telegram.core_client import TelegramCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("telegram")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so telegram plugin logs also go to supercc.log."""
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
    """Telegram 插件协程：在同进程 event loop 中运行。"""
    _setup_file_logging(data_dir)

    bot_token = config.channels.telegram.bot_token
    if not bot_token:
        raise RuntimeError("Telegram bot_token is required (configure via 'supercc config' interactive menu)")

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"Connecting to core at {core_url}")

    # 1. 创建 Telegram WS 客户端（接收消息）
    ws_client = TelegramWSClient(
        bot_token=bot_token,
        on_message=None,  # Will be set after core_client is created
    )

    # 2. 创建消息发送客户端
    telegram = TelegramClient(bot_token)

    # 3. 创建 Thin Client（连接 Core）
    core_client = TelegramCoreWSClient(
        core_url=core_url,
        ws_client=ws_client,
        telegram_client=telegram,
        bot_id=bot_token,  # Use bot_token as bot_id
        project_path=config.claude.approved_directory,
        groups=config.channels.telegram.groups,
        allowed_users=config.channels.telegram.allowed_users,
    )

    # ws_client 的消息回调指向 core_client.send_message
    ws_client._on_message = core_client.send_message

    # 连接到 Core
    try:
        await core_client.connect()
        logger.info("Connected to core")

        # 启动 Telegram WS 客户端（非阻塞）
        ws_client.start()
        logger.info("Telegram WS client started")

        # 保持进程运行，直到被取消
        while True:
            await asyncio.sleep(3600)
    finally:
        # 确保退出时关闭 ws_client
        logger.info("Shutting down Telegram client...")
        await ws_client.close()
        logger.info("Telegram client shutdown complete")


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
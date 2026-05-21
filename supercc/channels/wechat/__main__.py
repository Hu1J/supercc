"""微信个人（iLink）插件独立进程入口。

Usage:
    python -m supercc.channels.wechat
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
from supercc.channels.wechat.core_client import WeChatCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("wechat")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so wechat plugin logs also go to supercc.log."""
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        fh = logging.FileHandler(log_file, mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
        logger.debug("File logging added: %s", log_file)
    except Exception as e:
        logger.warning("Failed to add file logging: %s", e)


async def run_plugin(config, data_dir: str) -> None:
    """WeChat 插件协程：在同进程 event loop 中运行。"""
    _setup_file_logging(data_dir)

    # 检查 token 和 account_id
    wechat_cfg = config.channels.wechat
    if not wechat_cfg.token:
        raise RuntimeError("WeChat token is required (configure via QR scan)")
    if not wechat_cfg.account_id:
        raise RuntimeError("WeChat account_id is required (configure via QR scan)")

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info("Connecting to core at %s", core_url)

    # 创建 Thin Client
    core_client = WeChatCoreWSClient(
        core_url=core_url,
        token=wechat_cfg.token,
        account_id=wechat_cfg.account_id,
        bot_id=wechat_cfg.account_id,
        bot_openid=wechat_cfg.bot_open_id,
        data_dir=data_dir,
        project_path=config.claude.approved_directory,
        allowed_users=list(wechat_cfg.allowed_users) if wechat_cfg.allowed_users else [],
    )

    # 启动 Long Polling 和 WebSocket 连接
    await core_client.start()
    logger.info("WeChat Long Polling started")

    # 连接到 Core
    try:
        await core_client.connect()
        logger.info("Connected to core")

        # 保持进程运行，直到被取消
        while True:
            await asyncio.sleep(3600)
    finally:
        logger.info("Shutting down WeChat client...")
        await core_client.close()
        logger.info("WeChat client shutdown complete")


async def main() -> None:
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
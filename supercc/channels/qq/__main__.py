"""QQ 插件独立进程入口。

Usage:
    python -m supercc.channels.qq
    # 环境变量：
    #   SUPERCC_CONFIG=项目路径/.supercc/config.json
    #   SUPERCC_DATA=项目路径/.supercc/
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path

# 将项目根目录加入 sys.path（确保能 import supercc）
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from supercc.config import init_config, get_config
from supercc.channels.qq.client import QQClient, IncomingQQMessage
from supercc.channels.qq.ws_client import QQWSClient
from supercc.channels.qq.core_client import QQCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("qq")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so qq plugin logs also go to supercc.log."""
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        fh = logging.FileHandler(log_file, mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
        logger.debug("File logging added: %s", log_file)
    except Exception as e:
        logger.warning("Failed to add file logging: %s", e)


async def run_plugin(config, data_dir: str):
    """QQ 插件协程：在同进程 event loop 中运行。"""
    _setup_file_logging(data_dir)

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info("Connecting to core at %s", core_url)

    # 从 config 读取 QQ 配置
    qq_cfg = config.channels.qq
    app_id = qq_cfg.app_id
    app_secret = qq_cfg.app_secret
    bot_openid = qq_cfg.bot_openid

    if not app_id or not app_secret:
        raise RuntimeError("QQ app_id and app_secret are required (configure via `supercc install qq`)")

    # 1. 创建 QQ REST API 客户端（出站消息发送）
    qq_client = QQClient(
        app_id=app_id,
        app_secret=app_secret,
        bot_openid=bot_openid,
    )

    # 2. 创建 Thin Client（连接 Core）
    core_client = QQCoreWSClient(
        core_url=core_url,
        qq_client=qq_client,
        bot_openid=bot_openid,
        project_path=config.claude.approved_directory,
        groups=qq_cfg.groups,
        allowed_users=qq_cfg.allowed_users,
    )

    # 3. on_message 回调：在 QQWSClient 收到消息时，调用 core_client.send_message
    async def on_message(data: dict):
        incoming = QQClient.parse_incoming(data)
        if incoming is None:
            logger.warning("[QQ] Failed to parse incoming message: %s", data)
            return
        logger.info(
            "[QQ] Incoming: type=%s msg_id=%s author=%s content=%r",
            incoming.message_type, incoming.message_id[:20] if incoming.message_id else "None",
            incoming.author_id, incoming.content[:100] if incoming.content else "None"
        )
        try:
            await core_client.send_message(incoming)
        except Exception:
            logger.error("error in on_message\n%s", traceback.format_exc())

    # 4. 创建 QQ WebSocket Gateway 客户端（入站消息接收）
    ws_client = QQWSClient(
        app_id=app_id,
        app_secret=app_secret,
        on_message=on_message,
    )

    # 5. 连接到 Core
    await core_client.connect()
    logger.info("Connected to core")

    # 6. 启动 QQ WS 客户端
    await asyncio.to_thread(ws_client.start)
    logger.info("QQ WS client started")

    # 保持进程运行，直到被取消
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        logger.info("Shutting down QQ client...")
        await ws_client.close()
        await core_client.close()
        logger.info("QQ client shutdown complete")


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
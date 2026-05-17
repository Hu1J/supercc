"""钉钉插件独立进程入口。

Usage:
    python -m supercc.channels.dingtalk
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
from supercc.channels.dingtalk.client import DingTalkClient
from supercc.channels.dingtalk.ws_client import DingTalkWSClient
from supercc.channels.dingtalk.core_client import DingTalkCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("dingtalk")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so dingtalk plugin logs also go to supercc.log."""
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
    """钉钉插件协程：在同进程 event loop 中运行。"""
    # 添加文件日志
    _setup_file_logging(data_dir)

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"Connecting to core at {core_url}")

    # 创建客户端
    dingtalk_client = DingTalkClient(
        app_key=config.channels.dingtalk.app_key,
        app_secret=config.channels.dingtalk.app_secret,
    )

    core_client = DingTalkCoreWSClient(
        core_url=core_url,
        ws_client=None,  # 稍后设置
        dingtalk_client=dingtalk_client,
        bot_id=config.channels.dingtalk.app_key,
        project_path=config.claude.approved_directory,
        groups={},  # TODO: 从 config 读取群配置
        allowed_users=config.channels.dingtalk.allowed_users,
    )

    # 连接到 Core
    await core_client.connect()
    main_loop = asyncio.get_running_loop()
    logger.info("Connected to core")

    # on_message 回调在 dingtalk-stream 的 loop 中被调用，
    # 需要用 run_coroutine_threadsafe 桥接到主事件循环
    async def on_message(msg: dict):
        try:
            # 存储 session_webhook 到 core_client
            session_webhook = msg.get("session_webhook", "")
            chat_id = msg.get("chat_id", "")
            if session_webhook and chat_id:
                core_client.store_session_webhook(chat_id, session_webhook)

            fut = asyncio.run_coroutine_threadsafe(
                core_client.send_message(msg), main_loop
            )
            await asyncio.wrap_future(fut)
        except BaseException:
            logger.error("error in on_message\n%s", traceback.format_exc())

    ws_client = DingTalkWSClient(
        app_key=config.channels.dingtalk.app_key,
        app_secret=config.channels.dingtalk.app_secret,
        on_message=on_message,
    )

    # 将 ws_client 设置到 core_client
    core_client.ws_client = ws_client

    # 启动 WS 接收钉钉消息（dingtalk-stream SDK 用自己的 loop 阻塞）
    # 放到线程中执行，避免阻塞主事件循环
    await asyncio.to_thread(ws_client.start)


async def main():
    """独立进程入口（兼容旧模式）。"""
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

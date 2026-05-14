"""Feishu 插件独立进程入口。

Usage:
    python -m supercc.adapter.feishu
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
from supercc.adapter.feishu.client import FeishuClient
from supercc.adapter.feishu.ws_client import FeishuWSClient
from supercc.adapter.feishu.core_client import FeishuCoreWSClient
from supercc.main import ColoredFormatter, PlainFormatter

# 统一日志格式（与 core 保持一致）
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(ColoredFormatter())
logging.root.handlers = [_root_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("feishu")


def _setup_file_logging(data_dir: str) -> None:
    """Add file handler to root logger so feishu plugin logs also go to supercc.log."""
    log_file = os.path.join(data_dir, "supercc.log")
    try:
        fh = logging.FileHandler(log_file, mode="a")
        fh.setFormatter(PlainFormatter())
        logging.root.addHandler(fh)
        logger.debug("File logging added: %s", log_file)
    except Exception as e:
        logger.warning("Failed to add file logging: %s", e)


async def run_plugin(config, data_dir):
    """Feishu 插件协程：在同进程 event loop 中运行。"""
    # 添加文件日志（写入 supercc.log）
    _setup_file_logging(data_dir)

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"Connecting to core at {core_url}")

    feishu = FeishuClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
        bot_name=config.channels.feishu.bot_name,
        data_dir=data_dir,
    )

    core_client = FeishuCoreWSClient(
        core_url=core_url,
        feishu_client=feishu,
        bot_id=config.channels.feishu.bot_open_id,
        project_path=config.claude.approved_directory,
        groups=config.channels.feishu.groups,
        allowed_users=config.channels.feishu.allowed_users,
    )

    # 连接到 Core（在主事件循环中创建 WS 连接）
    await core_client.connect()
    main_loop = asyncio.get_running_loop()
    logger.info("Connected to core")

    # on_message 回调在 lark-oapi 的 loop 中被调用，
    # 需要用 run_coroutine_threadsafe 桥接到主事件循环
    async def on_message(msg):
        try:
            fut = asyncio.run_coroutine_threadsafe(
                core_client.send_message(msg), main_loop
            )
            await asyncio.wrap_future(fut)
        except BaseException:
            logger.error("error in on_message\n%s", traceback.format_exc())

    ws_client = FeishuWSClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
        bot_name=config.channels.feishu.bot_name,
        bot_open_id=config.channels.feishu.bot_open_id,
        domain=config.channels.feishu.domain,
        on_message=on_message,
        config_path=config.get("config_path", "") if hasattr(config, "get") else "",
    )

    # 启动 WS 接收飞书消息（lark-oapi 用自己的 loop 阻塞）
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

    config = init_config(config_path)
    # 给 config 附加 config_path，供 ws_client 使用
    config.config_path = config_path
    await run_plugin(config, data_dir)


if __name__ == "__main__":
    asyncio.run(main())

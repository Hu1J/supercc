"""Feishu 插件独立进程入口。

Usage:
    python -m supercc.plugin.feishu
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
from supercc.adapter.feishu.client import FeishuClient
from supercc.adapter.feishu.ws_client import FeishuWSClient
from supercc.adapter.feishu.core_client import FeishuCoreWSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("feishu-plugin")


async def main():
    config_path = os.environ.get("SUPERCC_CONFIG", "")
    data_dir = os.environ.get("SUPERCC_DATA", "")

    if not config_path:
        raise RuntimeError("SUPERCC_CONFIG environment variable is required")
    if not data_dir:
        raise RuntimeError("SUPERCC_DATA environment variable is required")

    config = init_config(config_path)

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"[FeishuPlugin] Connecting to core at {core_url}")

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

    async def on_message(msg):
        await core_client.send_message(msg)

    ws_client = FeishuWSClient(
        app_id=config.channels.feishu.app_id,
        app_secret=config.channels.feishu.app_secret,
        bot_name=config.channels.feishu.bot_name,
        bot_open_id=config.channels.feishu.bot_open_id,
        domain=config.channels.feishu.domain,
        on_message=on_message,
        config_path=config_path,
    )

    # 连接到 Core
    await core_client.connect()
    logger.info("[FeishuPlugin] Connected to core")

    # 启动 WS 接收飞书消息（阻塞）
    ws_client.start()


if __name__ == "__main__":
    asyncio.run(main())
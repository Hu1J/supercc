"""WeCom 插件独立进程入口。

Usage:
    python -m supercc.plugin.wecom
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
from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom.ws_client import WeComWSClient
from supercc.adapter.wecom.core_client import WeComCoreWSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("wecom-plugin")


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
    logger.info(f"[WeComPlugin] Connecting to core at {core_url}")

    wecom = WeComClient(
        corp_id=config.channels.wecom.corp_id,
        agent_id=config.channels.wecom.agent_id,
        corp_secret=config.channels.wecom.corp_secret,
    )

    core_client = WeComCoreWSClient(
        core_url=core_url,
        wecom_client=wecom,
        bot_id=config.channels.wecom.agent_id,
        project_path=config.claude.approved_directory,
        groups=config.channels.wecom.groups,
        allowed_users=config.channels.wecom.allowed_users,
    )

    async def on_message(msg):
        await core_client.send_message(msg)

    ws_client = WeComWSClient(
        bot_id=config.channels.wecom.agent_id,
        bot_secret=config.channels.wecom.corp_secret,
        on_message=on_message,
    )

    # 连接到 Core
    await core_client.connect()
    logger.info("[WeComPlugin] Connected to core")

    # 启动 WS 接收企微消息（阻塞）
    ws_client.start()


if __name__ == "__main__":
    asyncio.run(main())
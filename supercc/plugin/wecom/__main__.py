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

    # WebSocket 凭证：优先使用扫码接入获得的 bot_id/secret，
    # 回退到手动输入时的 agent_id/corp_secret（向后兼容）
    ws_bot_id = config.channels.wecom.bot_id or config.channels.wecom.agent_id
    ws_bot_secret = config.channels.wecom.secret or config.channels.wecom.corp_secret

    if not ws_bot_id or not ws_bot_secret:
        raise RuntimeError("WeCom bot_id and secret are required (configure via QR scan or manual input)")

    # 从 config 读取 core 端口
    core_port = config.core.port
    core_url = f"ws://127.0.0.1:{core_port}"
    logger.info(f"[WeComPlugin] Connecting to core at {core_url}")

    # 1. 创建 SDK WebSocket 客户端（接收 WeCom 消息）
    ws_client = WeComWSClient(
        bot_id=ws_bot_id,
        bot_secret=ws_bot_secret,
        on_message=None,  # 消息处理在 core_client 中
    )

    # 2. 创建消息发送客户端（基于 SDK WSClient）
    wecom = WeComClient(ws_client)

    # 3. 创建 Thin Client（连接 Core）
    core_client = WeComCoreWSClient(
        core_url=core_url,
        ws_client=ws_client,       # SDK WSClient（接收消息 + reply_stream）
        wecom_client=wecom,       # 消息发送
        bot_id=ws_bot_id,
        project_path=config.claude.approved_directory,
        groups=config.channels.wecom.groups,
        allowed_users=config.channels.wecom.allowed_users,
    )

    # ws_client 的消息回调指向 core_client.send_message
    ws_client._on_message = core_client.send_message

    # 连接到 Core
    await core_client.connect()
    logger.info("[WeComPlugin] Connected to core")

    # 启动 WS 接收企微消息（阻塞）
    ws_client.start()


if __name__ == "__main__":
    asyncio.run(main())
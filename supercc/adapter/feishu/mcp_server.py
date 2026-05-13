"""Plugin MCP Server — 暴露飞书特有工具给 core 调用。

Usage:
    python -m supercc.adapter.feishu.mcp_server --data-dir /path/to/data

子进程通过 --data-dir 共享 config.json，不直接传递凭证。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server

# 将项目根目录加入 sys.path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from supercc.adapter.feishu.client import FeishuClient
from supercc.adapter.feishu import mcp_tools


def main():
    """MCP server 入口。由 plugin 主进程 spawn 为子进程。"""
    # 解析 --data-dir 参数
    if "--data-dir" not in sys.argv:
        print("ERROR: --data-dir is required", file=sys.stderr)
        sys.exit(1)

    idx = sys.argv.index("--data-dir")
    data_dir = sys.argv[idx + 1]

    # 读取 config.json 获取凭证
    config_path = Path(data_dir) / "config.json"
    if not config_path.exists():
        print(f"ERROR: config.json not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    feishu_cfg = config.get("channels", {}).get("feishu", {})
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    bot_name = feishu_cfg.get("bot_name", "Claude")

    if not app_id or not app_secret:
        print("ERROR: feishu app_id or app_secret not configured", file=sys.stderr)
        sys.exit(1)

    # 创建 FeishuClient
    feishu = FeishuClient(
        app_id=app_id,
        app_secret=app_secret,
        bot_name=bot_name,
        data_dir=data_dir,
    )

    # 初始化 mcp_tools
    mcp_tools.init(feishu)

    # 创建并运行 MCP server
    server = create_sdk_mcp_server(
        name="SuperCC-Feishu",
        version="1.0.0",
        tools=[
            mcp_tools.feishu_send_file,
            mcp_tools.get_chat_members,
            mcp_tools.feishu_chat_history,
        ],
    )

    import asyncio
    asyncio.run(server.run(stdio=True))


if __name__ == "__main__":
    main()

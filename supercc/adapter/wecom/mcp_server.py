"""Plugin MCP Server — 暴露企业微信特有工具给 core 调用。

Usage:
    python -m supercc.adapter.wecom.mcp_server --data-dir /path/to/data

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

from supercc.adapter.wecom.client import WeComClient
from supercc.adapter.wecom import mcp_tools


def main():
    """MCP server 入口。由 plugin 主进程 spawn 为子进程。"""
    if "--data-dir" not in sys.argv:
        print("ERROR: --data-dir is required", file=sys.stderr)
        sys.exit(1)

    idx = sys.argv.index("--data-dir")
    data_dir = sys.argv[idx + 1]

    config_path = Path(data_dir) / "config.json"
    if not config_path.exists():
        print(f"ERROR: config.json not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    wecom_cfg = config.get("channels", {}).get("wecom", {})
    corp_id = wecom_cfg.get("corp_id", "")
    agent_id = wecom_cfg.get("agent_id", "")
    secret = wecom_cfg.get("secret", "")
    token = wecom_cfg.get("token", "")
    aes_key = wecom_cfg.get("aes_key", "")

    if not all([corp_id, agent_id, secret, token, aes_key]):
        print("ERROR: wecom corp_id, agent_id, secret, token, or aes_key not configured", file=sys.stderr)
        sys.exit(1)

    from wecom_aibot_sdk import WSClient

    ws_client = WSClient(
        corp_id=corp_id,
        agent_id=agent_id,
        secret=secret,
        token=token,
        aes_key=aes_key,
    )
    wecom = WeComClient(ws_client)

    mcp_tools.init(wecom)

    server = create_sdk_mcp_server(
        name="SuperCC-WeCom",
        version="1.0.0",
        tools=[
            mcp_tools.wecom_send_file,
        ],
    )

    import asyncio
    asyncio.run(server.run(stdio=True))


if __name__ == "__main__":
    main()

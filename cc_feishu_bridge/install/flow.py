"""Install flow state machine."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from cc_feishu_bridge.install.api import FeishuInstallAPI, AppRegistrationResult
from cc_feishu_bridge.install.qr import print_qr

logger = logging.getLogger(__name__)


async def run_install_flow(config_path: str = "config.yaml") -> AppRegistrationResult:
    """Run the full install flow: init → begin → QR → poll → save config."""
    print("\n🚀 开始安装 cc-feishu-bridge...\n")

    api = FeishuInstallAPI()
    try:
        # Step 1: Init
        print("正在初始化...")
        await api.init()
        print("初始化完成")

        # Step 2: Begin → get QR URL
        print("正在获取二维码...")
        begin_result = await api.begin()

        # Build QR URL with from=onboard tag
        qr_url = begin_result.verification_uri_complete
        if "?" in qr_url:
            qr_url += "&from=onboard"
        else:
            qr_url += "?from=onboard"

        # Step 3: Print QR
        print("\n" + "=" * 50)
        print("请使用飞书扫码完成配置（请确保已在飞书开放平台创建应用）")
        print("=" * 50 + "\n")
        print_qr(qr_url)
        print("等待扫码完成...\n")

        # Step 4: Poll for result
        result = await api.poll(begin_result.device_code, timeout=begin_result.expires_in)
    except RuntimeError as e:
        print(f"\n❌ 安装失败: {e}")
        raise
    finally:
        await api.close()

    # Step 5: Save config
    print(f"\n✅ 机器人创建成功！")

    save_config(result, config_path)
    return result


def save_config(result: AppRegistrationResult, config_path: str, bypass_accepted: bool = False) -> None:
    """Write the app credentials and defaults to config.yaml.

    Preserves existing 'groups' section so re-running
    the install flow does not wipe group registrations.
    """
    # Merge with existing config to preserve groups, etc.
    existing = {}
    if Path(config_path).exists():
        with open(config_path) as f:
            existing = yaml.safe_load(f) or {}

    config = {
        "feishu": {
            "app_id": result.app_id,
            "app_secret": result.app_secret,
            "bot_name": "Claude",
            "bot_open_id": "",  # auto-probed at startup; manual override goes here
            "domain": result.domain,
            # Preserve existing groups (auto-registered group chat configs)
            "groups": existing.get("feishu", {}).get("groups", {}),
        },
        "auth": {
            "allowed_users": [result.user_open_id],
        },
        "claude": {
            "cli_path": "claude",
            "max_turns": 50,
            "approved_directory": str(Path(config_path).resolve().parent.parent),
        },
        "storage": {
            "db_path": str(Path(config_path).resolve().parent / "sessions.db"),
        },
        "bypass_accepted": bypass_accepted,
    }

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print(f"\n✅ 配置已保存到 {config_path}")

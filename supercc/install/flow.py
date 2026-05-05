"""Install flow state machine."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from supercc.install.api import FeishuInstallAPI, AppRegistrationResult
from supercc.install.qr import print_qr

logger = logging.getLogger(__name__)


def save_config(result: AppRegistrationResult, config_path: str, bypass_accepted: bool = False) -> None:
    """Write the app credentials and defaults to config.yaml.

    Uses init_config + get_config + write_config to preserve existing settings
    (e.g. groups) when re-running the install flow.
    """
    from supercc.config import (
        init_config, get_config, write_config, _write_config_to_path,
        Config, ChannelsConfig, FeishuChannelConfig, AuthConfig, ClaudeConfig,
    )

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)

    # Fresh install (file doesn't exist or is empty): create new config directly
    if not Path(config_path).exists() or Path(config_path).stat().st_size == 0:
        cfg = Config(
            channels=ChannelsConfig(
                feishu=FeishuChannelConfig(
                    enabled=True,
                    app_id=result.app_id,
                    app_secret=result.app_secret,
                    bot_name="Claude",
                    bot_open_id="",
                    domain=result.domain,
                    groups={},
                    allowed_users=[result.user_open_id],
                ),
            ),
            auth=AuthConfig(),
            claude=ClaudeConfig(
                cli_path="claude",
                max_turns=50,
                approved_directory=str(Path(config_path).absolute().parent.parent),
            ),
            bypass_accepted=bypass_accepted,
        )
        _write_config_to_path(config_path, cfg)
        print(f"\n✅ 配置已保存到 {config_path}")
        return

    # Re-install or update: use singleton pattern to preserve existing groups
    init_config(config_path)
    cfg = get_config()
    cfg.channels.feishu.enabled = True
    cfg.channels.feishu.app_id = result.app_id
    cfg.channels.feishu.app_secret = result.app_secret
    cfg.channels.feishu.bot_name = "Claude"
    cfg.channels.feishu.bot_open_id = ""  # auto-probed at startup
    cfg.channels.feishu.domain = result.domain
    existing_users = set(cfg.channels.feishu.allowed_users)
    cfg.channels.feishu.allowed_users = list(existing_users | {result.user_open_id})
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")


async def run_install_flow(config_path: str = "config.yaml", bypass_accepted: bool = False) -> AppRegistrationResult:
    config_path = str(Path(config_path).absolute())
    """Run the full install flow: init → begin → QR → poll → save config."""
    print("\n🚀 开始安装 SuperCC...\n")

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
        logger.error(f"安装失败: {e}")
        print(f"\n❌ 安装失败: {e}")
        raise
    finally:
        await api.close()

    # Step 5: Save config
    print(f"\n✅ 机器人创建成功！")

    save_config(result, config_path, bypass_accepted=bypass_accepted)
    return result


def save_wecom_config(result, config_path: str, bypass_accepted: bool = False) -> None:
    """Write WeCom bot credentials to config.json."""
    from supercc.config import (
        init_config, get_config, write_config, _write_config_to_path,
        Config, ChannelsConfig, FeishuChannelConfig, WeComChannelConfig, AuthConfig, ClaudeConfig,
    )

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(config_path).exists() or Path(config_path).stat().st_size == 0:
        cfg = Config(
            channels=ChannelsConfig(
                feishu=FeishuChannelConfig(),
                wecom=WeComChannelConfig(
                    enabled=True,
                    bot_id=result.bot_id,
                    bot_secret=result.bot_secret,
                    bot_name="Claude",
                    groups={},
                    allowed_users=[],
                ),
            ),
            auth=AuthConfig(),
            claude=ClaudeConfig(
                cli_path="claude",
                max_turns=50,
                approved_directory=str(Path(config_path).absolute().parent.parent),
            ),
            bypass_accepted=bypass_accepted,
        )
        _write_config_to_path(config_path, cfg)
        print(f"\n✅ 企业微信配置已保存到 {config_path}")
        return

    init_config(config_path)
    cfg = get_config()
    cfg.channels.wecom.enabled = True
    cfg.channels.wecom.bot_id = result.bot_id
    cfg.channels.wecom.bot_secret = result.bot_secret
    cfg.channels.wecom.bot_name = "Claude"
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 企业微信配置已保存到 {config_path}")


async def run_wecom_install_flow(config_path: str = "config.yaml", bypass_accepted: bool = False):
    """Run the full WeCom install flow: generate QR -> scan -> poll -> save config."""
    config_path = str(Path(config_path).absolute())
    print("\n🚀 开始安装企业微信机器人...\n")

    from supercc.install.api import WeComInstallAPI
    from supercc.install.qr import print_qr

    api = WeComInstallAPI()
    try:
        print("正在获取二维码...")
        gen = await api.generate()
        scode = gen.get("scode", "")
        auth_url = gen.get("auth_url", "")
        if not scode or not auth_url:
            raise RuntimeError(f"无法获取二维码: {gen}")

        print("\n" + "=" * 50)
        print("请使用企业微信扫码完成配置")
        print("=" * 50 + "\n")
        print_qr(auth_url)
        print("等待扫码完成...\n")

        result = await api.poll(scode, timeout=600)
    except RuntimeError as e:
        logger.error(f"安装失败: {e}")
        print(f"\n❌ 安装失败: {e}")
        raise
    finally:
        await api.close()

    print(f"\n✅ 企业微信机器人创建成功！")
    save_wecom_config(result, config_path, bypass_accepted=bypass_accepted)
    return result

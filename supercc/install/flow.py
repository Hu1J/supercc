"""Install flow state machine."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from supercc.install.api import FeishuInstallAPI, AppRegistrationResult
from supercc.install.qr import print_qr

logger = logging.getLogger(__name__)


def save_config(result: AppRegistrationResult, config_path: str, bypass_accepted: bool = False) -> None:
    """Write the app credentials and defaults to config.json.

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


async def run_install_flow(config_path: str = "config.json", bypass_accepted: bool = False) -> AppRegistrationResult | None:
    """Run the full install flow: init → begin → QR → poll → save config.

    支持扫码创建和手动输入两种方式。
    """
    config_path = str(Path(config_path).absolute())

    import questionary

    method = questionary.select(
        "请选择飞书机器人接入方式：",
        choices=[
            questionary.Choice("🔍 扫码创建（推荐）", value="qrcode"),
            questionary.Choice("⌨️  手动输入 App ID 和 App Secret", value="manual"),
        ],
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask()

    if method == "manual":
        print("\n📋 手动配置飞书机器人\n")
        print("请将已有的飞书应用凭证填入以下内容：\n")
        app_id = questionary.text("App ID", style=questionary.Style([("input", "fg:#CCCCCC")])).ask() or ""
        app_secret = questionary.password("App Secret", style=questionary.Style([("password", "fg:#CCCCCC")])).ask() or ""
        bot_name = questionary.text("机器人名称（如 Claude）", default="Claude", style=questionary.Style([("input", "fg:#CCCCCC")])).ask() or "Claude"

        if not app_id or not app_secret:
            print("\n❌ App ID 和 App Secret 不能为空\n")
            return None

        class _ManualResult:
            app_id = app_id
            app_secret = app_secret
            bot_name = bot_name
            domain = ""
            user_open_id = ""
        result = _ManualResult()
        save_config(result, config_path, bypass_accepted=bypass_accepted)
        return result

    print("\n🚀 扫码创建飞书机器人...\n")

    api = FeishuInstallAPI()
    try:
        await api.init()
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
        print(f"链接扫码（终端二维码无法扫描时使用）: {qr_url}")
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

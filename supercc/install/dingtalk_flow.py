"""钉钉安装流程。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_dingtalk_install_flow(config_path: str = "config.json", bypass_accepted: bool = False) -> None:
    """Run the DingTalk install flow.

    用户在钉钉开放平台创建企业内部机器人后，
    输入 AppKey 和 AppSecret 进行配置。

    Steps:
    1. 引导用户在钉钉开放平台创建机器人
    2. 获取 AppKey 和 AppSecret
    3. 保存到 config
    """
    import questionary

    config_path = str(Path(config_path).absolute())

    print("\n" + "=" * 50)
    print("钉钉智能机器人配置")
    print("=" * 50 + "\n")

    print("请按以下步骤获取钉钉机器人凭证：\n")
    print("1. 打开钉钉开放平台（https://open.dingtalk.com/）")
    print("2. 进入「应用开发」→「钉钉应用」")
    print("3. 点击「创建应用」，选择「企业内部开发」")
    print("4. 选择「机器人」类型")
    print("5. 配置机器人名称和描述")
    print("6. 在「机器人」配置页面，找到「AppKey」和「AppSecret」")
    print("7. 在「消息订阅」中启用消息接收")
    print("8. 配置「Stream 模式」为启用（推荐）\n")
    print("=" * 50 + "\n")

    app_key = questionary.text("请输入钉钉 AppKey:").ask()
    app_secret = questionary.password("请输入钉钉 AppSecret:").ask()

    if not app_key or not app_secret:
        print("\n❌ AppKey 和 AppSecret 都不能为空，取消配置")
        return

    save_dingtalk_config(
        app_key=app_key,
        app_secret=app_secret,
        config_path=config_path,
        bypass_accepted=bypass_accepted,
    )


def save_dingtalk_config(
    app_key: str = "",
    app_secret: str = "",
    config_path: str = "",
    bypass_accepted: bool = False,
) -> None:
    """Write DingTalk credentials to config.

    Uses init_config + get_config + write_config to preserve existing settings
    (e.g. feishu config, groups) when re-running the install flow.
    """
    from supercc.config import (
        init_config, get_config, write_config,
        Config, ChannelsConfig, FeishuChannelConfig, DingTalkChannelConfig,
        WeComChannelConfig, AuthConfig, ClaudeConfig,
    )

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(config_path).exists() or Path(config_path).stat().st_size == 0:
        cfg = Config(
            channels=ChannelsConfig(
                feishu=FeishuChannelConfig(enabled=False),
                dingtalk=DingTalkChannelConfig(
                    enabled=True,
                    app_key=app_key,
                    app_secret=app_secret,
                    allowed_users=[],
                ),
                wecom=WeComChannelConfig(enabled=False),
            ),
            auth=AuthConfig(),
            claude=ClaudeConfig(
                cli_path="claude",
                max_turns=50,
                approved_directory=str(Path(config_path).absolute().parent.parent),
            ),
            bypass_accepted=bypass_accepted,
        )
        from supercc.config import _write_config_to_path
        _write_config_to_path(config_path, cfg)
        print(f"\n✅ 配置已保存到 {config_path}")
        return

    init_config(config_path)
    cfg = get_config()
    cfg.channels.dingtalk.enabled = True
    cfg.channels.dingtalk.app_key = app_key
    cfg.channels.dingtalk.app_secret = app_secret
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")

"""WeCom 安装流程。"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def save_wecom_config(
    corp_id: str,
    agent_id: str,
    corp_secret: str,
    config_path: str,
    bypass_accepted: bool = False,
) -> None:
    """Write WeCom credentials to config.

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
                dingtalk=DingTalkChannelConfig(enabled=False),
                wecom=WeComChannelConfig(
                    enabled=True,
                    corp_id=corp_id,
                    agent_id=agent_id,
                    corp_secret=corp_secret,
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
        from supercc.config import _write_config_to_path
        _write_config_to_path(config_path, cfg)
        print(f"\n✅ 配置已保存到 {config_path}")
        return

    init_config(config_path)
    cfg = get_config()
    cfg.channels.wecom.enabled = True
    cfg.channels.wecom.corp_id = corp_id
    cfg.channels.wecom.agent_id = agent_id
    cfg.channels.wecom.corp_secret = corp_secret
    cfg.channels.wecom.bot_name = "Claude"
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")


def run_wecom_install_flow(config_path: str = "config.yaml", bypass_accepted: bool = False) -> None:
    """Run the WeCom install flow: prompt for credentials → save config.

    WeCom 没有类似飞书的 OAuth device flow，需要用户手动在企业微信管理后台
    创建智能机器人（API 模式 + 长连接），然后把 Bot ID 和 Secret 告诉 SuperCC。

    步骤：
    1. 打开企业微信客户端 → 工作台 → 智能机器人 → 创建机器人
    2. 选择「手动创建」，滑动到底部选择「API 模式创建」
    3. 连接方式选择「使用长连接方式」
    4. 复制 Bot ID，点击「获取」获取 Secret
    5. 配置可见范围并保存
    6. 把 Bot ID 和 Secret 输入到这里
    """
    config_path = str(Path(config_path).absolute())

    print("\n" + "=" * 50)
    print("企业微信智能机器人配置")
    print("=" * 50 + "\n")

    print("请按以下步骤获取企业微信机器人凭证：\n")
    print("1. 打开企业微信客户端 → 工作台 → 智能机器人 → 创建机器人")
    print("2. 点击「手动创建」，滑动到底部选择「API 模式创建」")
    print("3. 连接方式选择「使用长连接方式」")
    print("4. 复制生成的 Bot ID，点击「获取」获取 Secret")
    print("5. 配置「可见范围」后点击「保存」\n")
    print("=" * 50 + "\n")

    import questionary

    corp_id = questionary.text("请输入企业微信 CorpID:").ask()
    agent_id = questionary.text("请输入企业微信 AgentID (BotID):").ask()
    corp_secret = questionary.password("请输入企业微信 Secret (机器人密钥):").ask()

    if not corp_id or not agent_id or not corp_secret:
        print("\n❌ 所有字段都不能为空，取消配置")
        return

    save_wecom_config(
        corp_id=corp_id,
        agent_id=agent_id,
        corp_secret=corp_secret,
        config_path=config_path,
        bypass_accepted=bypass_accepted,
    )

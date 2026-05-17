"""QQ 安装流程。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def save_qq_config(
    app_id: str = "",
    app_secret: str = "",
    bot_openid: str = "",
    config_path: str = "",
    bypass_accepted: bool = False,
) -> None:
    """Write QQ credentials to config.

    Uses init_config + get_config + write_config to preserve existing settings
    (e.g. feishu config, groups) when re-running the install flow.
    """
    from supercc.config import (
        init_config, get_config, write_config,
        Config, ChannelsConfig, FeishuChannelConfig, DingTalkChannelConfig,
        WeComChannelConfig, TelegramChannelConfig,
        QQChannelConfig, WhatsAppChannelConfig, WeChatChannelConfig,
        AuthConfig, ClaudeConfig,
    )

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(config_path).exists() or Path(config_path).stat().st_size == 0:
        cfg = Config(
            channels=ChannelsConfig(
                feishu=FeishuChannelConfig(enabled=False),
                dingtalk=DingTalkChannelConfig(enabled=False),
                wecom=WeComChannelConfig(enabled=False),
                telegram=TelegramChannelConfig(enabled=False),
                qq=QQChannelConfig(
                    enabled=True,
                    app_id=app_id,
                    app_secret=app_secret,
                    bot_openid=bot_openid,
                    groups={},
                    allowed_users=[],
                ),
                whatsapp=WhatsAppChannelConfig(enabled=False),
                wechat=WeChatChannelConfig(enabled=False),
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
    cfg.channels.qq.enabled = True
    cfg.channels.qq.app_id = app_id
    cfg.channels.qq.app_secret = app_secret
    cfg.channels.qq.bot_openid = bot_openid
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")


def run_qq_install_flow(config_path: str = "config.json", bypass_accepted: bool = False) -> None:
    """Run the QQ install flow.

    Steps:
    1. User creates app at https://q.qq.com
    2. Apply for robot capability
    3. Get AppID and AppSecret
    4. Save to config
    """
    try:
        import questionary
    except ImportError:
        print("❌ 需要 questionary 库来运行安装向导")
        print("请运行: pip install questionary")
        sys.exit(1)

    config_path = str(Path(config_path).absolute())

    print("\n" + "=" * 50)
    print("QQ 智能机器人配置")
    print("=" * 50 + "\n")

    print("请按以下步骤获取 QQ 机器人凭证：\n")
    print("1. 打开 https://q.qq.com 并登录")
    print("2. 进入「应用管理」→「创建应用」")
    print("3. 选择「机器人」类型")
    print("4. 完成基础信息填写后，在「开发管理」中启用「机器人」能力")
    print("5. 在「开发管理」→「开发配置」中获取：")
    print("   - AppID（应用 ID）")
    print("   - AppSecret（应用密钥）")
    print("6. 配置消息接收权限（私聊消息、群聊消息）")
    print("7. 将机器人添加到你想要使用的群/私聊中\n")
    print("=" * 50 + "\n")

    app_id = questionary.text("请输入 QQ App ID:").ask()
    app_secret = questionary.password("请输入 QQ App Secret:").ask()
    bot_openid = questionary.text("请输入机器人 Bot OpenID（可选，留空自动发现）:").ask() or ""

    if not app_id or not app_secret:
        print("\n❌ App ID 和 App Secret 都不能为空，取消配置")
        return

    save_qq_config(
        app_id=app_id,
        app_secret=app_secret,
        bot_openid=bot_openid,
        config_path=config_path,
        bypass_accepted=bypass_accepted,
    )

    print("\n✅ QQ 机器人配置完成！")
    print("\n启动命令：")
    print("  SUPERCC_CONFIG=config.json SUPERCC_DATA=.supercc python -m supercc.channels.qq")
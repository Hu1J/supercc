"""WeCom 安装流程。"""
from __future__ import annotations

import asyncio
import logging
import platform as os_platform
import sys
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================================
# WeCom AI Bot QR Code Scan API
# ============================================================================

QR_GENERATE_URL = "https://work.weixin.qq.com/ai/qc/generate"
QR_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"
POLL_INTERVAL = 3  # seconds
POLL_TIMEOUT = 300  # 5 minutes


def _get_plat_code() -> int:
    """Map OS platform to WeCom plat code."""
    p = os_platform.platform().lower()
    if "darwin" in p or "mac" in p:
        return 1
    if "win" in p:
        return 2
    if "linux" in p:
        return 3
    return 0


@dataclass
class WeComBotInfo:
    bot_id: str
    secret: str


async def _https_get(url: str) -> dict:
    """Sync HTTP GET (runs in thread pool to avoid blocking)."""
    def _fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "SuperCC/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def generate_qr_code() -> tuple[str, str]:
    """Fetch QR code URL and scode from WeCom.

    Returns:
        (scode, auth_url)
    """
    plat = _get_plat_code()
    url = f"{QR_GENERATE_URL}?source=wecom-cli&plat={plat}"
    raw = await _https_get(url)
    resp = _parse_json(raw)
    scode = resp.get("data", {}).get("scode", "")
    auth_url = resp.get("data", {}).get("auth_url", "")
    if not scode or not auth_url:
        raise RuntimeError(f"获取二维码失败，响应: {raw[:200]}")
    return scode, auth_url


async def poll_scan_result(scode: str) -> WeComBotInfo:
    """Poll until QR code is scanned and bot info is returned.

    Args:
        scode: The code returned from generate_qr_code()

    Returns:
        WeComBotInfo with bot_id and secret

    Raises:
        TimeoutError: If user doesn't scan within POLL_TIMEOUT seconds
    """
    url = f"{QR_QUERY_URL}?scode={urllib.parse.quote(scode)}"
    start = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed >= POLL_TIMEOUT:
            raise TimeoutError("扫码超时（5 分钟），请重试。")

        raw = await _https_get(url)
        resp = _parse_json(raw)
        status = resp.get("data", {}).get("status", "")

        if status == "success":
            bot_info = resp.get("data", {}).get("bot_info", {})
            bot_id = bot_info.get("botid", "")
            secret = bot_info.get("secret", "")
            if not bot_id or not secret:
                raise RuntimeError(f"扫码成功但未获取到 Bot 信息: {raw[:200]}")
            return WeComBotInfo(bot_id=bot_id, secret=secret)

        # Still waiting
        await asyncio.sleep(POLL_INTERVAL)


def _parse_json(raw: str) -> dict:
    try:
        import json as _json
        return _json.loads(raw)
    except Exception:
        return {}


def render_qr_ascii(auth_url: str) -> None:
    """Render QR code in terminal using ASCII art."""
    import qrcode
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=2)
    qr.add_data(auth_url)
    qr.make(fit=True)
    print()
    qr.print_ascii()
    print()


# ============================================================================
# Config saving
# ============================================================================

def save_wecom_config(
    corp_id: str = "",
    agent_id: str = "",
    corp_secret: str = "",
    bot_id: str = "",
    secret: str = "",
    config_path: str = "",
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
                    bot_id=bot_id,
                    secret=secret,
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
    cfg.channels.wecom.bot_id = bot_id
    cfg.channels.wecom.secret = secret
    cfg.channels.wecom.bot_name = "Claude"
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")


# ============================================================================
# Install flow
# ============================================================================

def run_wecom_install_flow(config_path: str = "config.json", bypass_accepted: bool = False) -> None:
    """Run the WeCom install flow.

    Offers two options:
    1. QR code scan (recommended) — uses WeCom's ai/qc API to auto-create bot
    2. Manual input — user enters Bot ID and Secret from WeCom management console

    Steps for QR scan:
    1. Fetch QR code from WeCom
    2. Render in terminal
    3. Poll for scan result
    4. Save bot_id + secret to config

    Steps for manual:
    1. User creates bot in WeCom management console
    2. Enter Bot ID and Secret
    3. Save to config
    """
    import questionary

    config_path = str(Path(config_path).absolute())

    print("\n" + "=" * 50)
    print("企业微信智能机器人配置")
    print("=" * 50 + "\n")

    method = questionary.select(
        "请选择接入方式：",
        choices=[
            questionary.Choice("🔍 扫码接入（推荐）", value="qrcode"),
            questionary.Choice("⌨️  手动输入 Bot ID 和 Secret", value="manual"),
        ],
        style=questionary.Style([
            ("selected", "fg:#00AA00 bold"),
            ("choice", "fg:#CCCCCC"),
            ("pointer", "fg:#00AA00 bold"),
        ]),
    ).ask()

    bot_id = ""
    secret = ""
    corp_id = ""
    agent_id = ""
    corp_secret = ""

    if method == "qrcode":
        print("\n正在获取二维码...\n")

        try:
            scode, auth_url = asyncio.run(generate_qr_code())
        except Exception as e:
            print(f"\n❌ 获取二维码失败：{e}")
            print("\n请尝试手动输入 Bot ID 和 Secret。\n")
            method = "manual"
        else:
            print("请使用企业微信扫码以下二维码：\n")
            render_qr_ascii(auth_url)
            print(f"也可打开二维码链接扫码: https://work.weixin.qq.com/ai/qc/gen?source=wecom-cli&scode={scode}")
            print("等待扫码中...\n")

            try:
                bot_info = asyncio.run(poll_scan_result(scode))
                bot_id = bot_info.bot_id
                secret = bot_info.secret
                print(f"\n✅ 扫码成功！Bot ID: {bot_id}\n")
            except TimeoutError:
                print("\n❌ 扫码超时，请重试。\n")
                return
            except Exception as e:
                print(f"\n❌ 获取 Bot 信息失败：{e}")
                return

    if method == "manual":
        print("请按以下步骤获取企业微信机器人凭证：\n")
        print("1. 打开企业微信客户端 → 工作台 → 智能机器人 → 创建机器人")
        print("2. 点击「手动创建」，滑动到底部选择「API 模式创建」")
        print("3. 连接方式选择「使用长连接方式」")
        print("4. 复制生成的 Bot ID，点击「获取」获取 Secret")
        print("5. 配置「可见范围」后点击「保存」\n")
        print("=" * 50 + "\n")

        bot_id = questionary.text("请输入企业微信机器人 Bot ID:").ask()
        secret = questionary.password("请输入企业微信机器人 Secret:").ask()

        if not bot_id or not secret:
            print("\n❌ Bot ID 和 Secret 都不能为空，取消配置")
            return

    save_wecom_config(
        corp_id=corp_id,
        agent_id=agent_id,
        corp_secret=corp_secret,
        bot_id=bot_id,
        secret=secret,
        config_path=config_path,
        bypass_accepted=bypass_accepted,
    )

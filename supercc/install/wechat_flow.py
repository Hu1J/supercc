"""微信个人（iLink）安装流程 — QR 扫码接入。"""
from __future__ import annotations

import asyncio
import json
import secrets
import struct
import base64
import time
from pathlib import Path
from dataclasses import dataclass

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
QR_TIMEOUT_MS = 35_000
BOT_TYPE = "3"  # 3 = 个人微信机器人


def _make_ssl_connector():
    """Return a TCPConnector with certifi CA bundle, or None if unavailable.

    iLink server (ilinkai.weixin.qq.com) is not verifiable against some system
    CA stores (notably Homebrew's OpenSSL on macOS Apple Silicon).
    """
    try:
        import ssl
        import certifi
        import aiohttp
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


@dataclass
class WeChatCredentials:
    account_id: str
    token: str
    bot_open_id: str
    base_url: str


async def _api_get(session, *, base_url: str, endpoint: str, timeout_ms: int = QR_TIMEOUT_MS) -> dict:
    """GET iLink API，base_url 可动态指定。"""
    import aiohttp
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"iLink GET {endpoint} 网络错误 ({type(exc).__name__}): {exc}") from exc


async def qr_login(timeout_seconds: int = 480) -> WeChatCredentials | None:
    """交互式微信扫码登录。

    1. 获取二维码
    2. 终端渲染 QR
    3. 轮询扫码状态
    4. 扫码确认后返回凭证
    """
    import aiohttp

    connector = _make_ssl_connector()
    async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
        # 获取二维码
        current_base_url = ILINK_BASE_URL
        try:
            qr_resp = await _api_get(
                session,
                base_url=current_base_url,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={BOT_TYPE}",
            )
        except Exception as exc:
            print(f"\n❌ 获取二维码失败：{exc}")
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            print(f"\n❌ 获取二维码失败，响应缺少 qrcode 字段")
            return None

        # qrcode_url 是可扫描的完整链接
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value

        print("\n请使用微信扫描以下二维码：")
        if qrcode_url:
            print(qrcode_url)

        # ASCII QR 渲染
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception as exc:
            print(f"（终端二维码渲染失败，请直接使用上面的链接扫码）")

        print()

        deadline = time.monotonic() + timeout_seconds
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                print(f"\n轮询异常: {exc}，1秒后重试...")
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")

            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qr_resp = await _api_get(session, base_url=ILINK_BASE_URL, endpoint=f"{EP_GET_BOT_QR}?bot_type={BOT_TYPE}")
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    qr_scan_data = qrcode_url if qrcode_url else qrcode_value
                    if qrcode_url:
                        print(qrcode_url)
                    try:
                        import qrcode as _qr
                        q = _qr.QRCode()
                        q.add_data(qr_scan_data)
                        q.make(fit=True)
                        q.print_ascii(invert=True)
                    except Exception:
                        pass
                except Exception as exc:
                    print(f"\n刷新二维码失败: {exc}")
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    print(f"\n❌ 扫码成功但凭证不完整: {status_resp}")
                    return None
                print(f"\n✅ 微信连接成功！account_id={account_id}")
                return WeChatCredentials(
                    account_id=account_id,
                    token=token,
                    bot_open_id=user_id,
                    base_url=base_url,
                )
            else:
                print(f"  状态: {status}")

            await asyncio.sleep(1)

        print("\n微信登录超时。")
        return None


def save_wechat_config(
    account_id: str = "",
    token: str = "",
    bot_open_id: str = "",
    config_path: str = "",
    bypass_accepted: bool = False,
) -> None:
    """保存微信凭证到配置文件。"""
    from supercc.config import (
        init_config, get_config, write_config, _write_config_to_path,
        Config, ChannelsConfig, FeishuChannelConfig, DingTalkChannelConfig,
        WeComChannelConfig, WeChatChannelConfig, AuthConfig, ClaudeConfig,
    )

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(config_path).exists() or Path(config_path).stat().st_size == 0:
        cfg = Config(
            channels=ChannelsConfig(
                feishu=FeishuChannelConfig(enabled=False),
                dingtalk=DingTalkChannelConfig(enabled=False),
                wecom=WeComChannelConfig(enabled=False),
                wechat=WeChatChannelConfig(
                    enabled=True,
                    account_id=account_id,
                    token=token,
                    bot_open_id=bot_open_id,
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

    init_config(config_path)
    cfg = get_config()
    cfg.channels.wechat.enabled = True
    cfg.channels.wechat.account_id = account_id
    cfg.channels.wechat.token = token
    cfg.channels.wechat.bot_open_id = bot_open_id
    cfg.claude.cli_path = "claude"
    cfg.claude.max_turns = 50
    cfg.claude.approved_directory = str(Path(config_path).absolute().parent.parent)
    cfg.bypass_accepted = bypass_accepted
    write_config(cfg)
    print(f"\n✅ 配置已保存到 {config_path}")


async def run_wechat_install_flow(config_path: str = "config.json", bypass_accepted: bool = False) -> None:
    """运行微信安装流程（扫码登录 + 保存配置）。"""
    config_path = str(Path(config_path).absolute())

    print("\n" + "=" * 50)
    print("微信个人号智能机器人配置")
    print("=" * 50 + "\n")

    print("请使用微信扫描以下二维码绑定机器人账号。\n")

    try:
        creds = await qr_login()
    except Exception as e:
        print(f"\n❌ 微信登录失败：{e}")
        return

    if not creds:
        print("\n❌ 微信登录失败或超时")
        return

    save_wechat_config(
        account_id=creds.account_id,
        token=creds.token,
        bot_open_id=creds.bot_open_id,
        config_path=config_path,
        bypass_accepted=bypass_accepted,
    )

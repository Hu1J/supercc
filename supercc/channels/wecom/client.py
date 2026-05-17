"""企业微信消息发送客户端（aiohttp 直连实现）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WeComClient:
    """
    企业微信消息发送客户端。

    封装 WeComWSClient，提供：
    - send_text / send_markdown：主动发送文本消息
    - send_template_card：发送模板卡片
    """

    def __init__(self, ws_client):
        """
        Args:
            ws_client: WeComWSClient 实例（aiohttp-based）
        """
        self._ws = ws_client

    # ── 主动发送 ────────────────────────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str) -> str:
        """发送文本消息（主动发送，无原始帧）。"""
        ack = await self._ws.send_text(chat_id, text)
        return ack.get("body", {}).get("msgid", "") if ack else ""

    async def send_markdown(self, chat_id: str, content: str) -> str:
        """发送 Markdown 消息（主动发送，无原始帧）。"""
        ack = await self._ws.send_markdown(chat_id, content)
        return ack.get("body", {}).get("msgid", "") if ack else ""

    async def send_template_card(
        self, chat_id: str, card_type: str, title: str, desc: str = "", buttons: list | None = None
    ) -> str:
        """发送模板卡片（主动发送）。"""
        card = self._build_card(card_type, title, desc, buttons)
        return await self._send_card(chat_id, card)

    # ── 专用业务消息 ─────────────────────────────────────────────────────────

    async def send_authorization_card(self, chat_id: str, reason: str) -> str:
        """发送权限不足引导卡片。"""
        logger.info(f"[WeComClient] send_authorization_card: chat_id={chat_id}, reason_len={len(reason)}")
        return await self.send_template_card(
            chat_id=chat_id,
            card_type="button_interaction",
            title="权限不足",
            desc=reason,
            buttons=[{"text": "联系管理员", "key": "contact_admin"}],
        )

    async def send_typing_indicator(self, chat_id: str) -> str:
        """发送'正在思考...'提示（text_notice 模板卡片）。"""
        card = self._build_card("text_notice", "正在思考...", "", source={"desc": "SuperCC"})
        return await self._send_card(chat_id, card)

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _build_card(
        self,
        card_type: str,
        title: str,
        desc: str = "",
        buttons: list | None = None,
        source: dict | None = None,
    ) -> dict:
        """Build a template card dict matching WeCom's expected format."""
        card = {
            "card_type": card_type,
            "main_title": {"title": title},
        }
        if desc:
            card["main_title"]["desc"] = desc
        if source:
            card["source"] = source
        if buttons:
            card["button_list"] = [
                {"text": btn.get("text", ""), "key": btn.get("key", btn.get("text", ""))}
                for btn in buttons
            ]
        return card

    async def _send_card(self, chat_id: str, card: dict) -> str:
        """Send a template card message via aibot_send_msg."""
        logger.info(f"[WeComClient] _send_card: chat_id={chat_id}, card_type={card.get('card_type')}, title={card.get('main_title', {}).get('title', '')}")
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="template_card",
            template_card=card,
        )
        errcode = ack.get("errcode", -1) if ack else -1
        errmsg = ack.get("errmsg", "no ack") if ack else "no ack"
        logger.info(f"[WeComClient] _send_card ack: errcode={errcode}, errmsg={errmsg}")
        return ack.get("body", {}).get("msgid", "") if ack else ""

    # ── 媒体 ─────────────────────────────────────────────────────────────────

    async def upload_media(self, file_path: str) -> str:
        """上传本地文件，返回 media_id。"""
        import aiohttp
        # WeCom media upload via HTTP POST
        # This is a simplified implementation - full implementation would need
        # the actual upload URL and authentication
        async with aiohttp.ClientSession() as session:
            # Placeholder - actual implementation depends on WeCom media upload API
            raise NotImplementedError("upload_media requires WeCom media API integration")

    async def send_file(self, chat_id: str, media_id: str) -> str:
        """发送文件消息。"""
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="file",
            file={"media_id": media_id},
        )
        return ack.get("body", {}).get("msgid", "") if ack else ""

    async def send_image(self, chat_id: str, media_id: str) -> str:
        """发送图片消息。"""
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="image",
            image={"media_id": media_id},
        )
        return ack.get("body", {}).get("msgid", "") if ack else ""

    # ── 下载 ─────────────────────────────────────────────────────────────────

    async def download_file(self, url: str, aes_key: str = "") -> bytes:
        """下载并解密文件。"""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()
        if aes_key:
            from Crypto.Cipher import AES
            import base64
            key = base64.b64decode(aes_key)
            cipher = AES.new(key, AES.MODE_CBC, b"\0" * 16)
            data = cipher.decrypt(data)
            # Remove PKCS7 padding
            pad = data[-1]
            data = data[:-pad]
        return data

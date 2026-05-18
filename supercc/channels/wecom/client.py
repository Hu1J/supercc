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

    async def send_text(self, chat_id: str, text: str) -> dict:
        """发送文本消息（主动发送，fire-and-forget）。"""
        return await self._ws.send_text(chat_id, text)

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        """发送 Markdown 消息（主动发送，fire-and-forget）。"""
        return await self._ws.send_markdown(chat_id, content)

    async def send_template_card(
        self, chat_id: str, card_type: str, title: str, desc: str = "", buttons: list | None = None
    ) -> dict:
        """发送模板卡片（主动发送）。"""
        card = self._build_card(card_type, title, desc, buttons)
        return await self._send_card(chat_id, card)

    # ── 专用业务消息 ─────────────────────────────────────────────────────────

    async def send_authorization_card(self, chat_id: str, reason: str, reply_req_id: str = "") -> str:
        """发送权限不足引导卡片（fire-and-forget，不等待回调）。"""
        logger.info(f"[WeComClient] send_authorization_card: chat_id={chat_id}, reason_len={len(reason)}, reply_req_id={reply_req_id[:20] if reply_req_id else 'None'}")
        text = f"🔒 **权限不足**\n\n{reason}"
        return await self.send_markdown(chat_id, text)

    async def reply_text(self, reply_req_id: str, content: str) -> str:
        """通过 APP_CMD_RESPONSE (reply) 发送文本消息。"""
        try:
            ack = await self._ws.reply_text(reply_req_id=reply_req_id, content=content)
            logger.info(f"[WeComClient] reply_text ack: errcode={ack.get('errcode')}, errmsg={ack.get('errmsg')}")
            return "ok" if ack.get("errcode") == 0 else f"err: {ack.get('errmsg')}"
        except Exception as e:
            logger.error(f"[WeComClient] reply_text failed: {e}")
            return f"err: {e}"

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

    async def _send_card(self, chat_id: str, card: dict) -> dict:
        """Send a template card message via aibot_send_msg."""
        logger.info(f"[WeComClient] _send_card: chat_id={chat_id}, card_type={card.get('card_type')}, title={card.get('main_title', {}).get('title', '')}")
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="template_card",
            template_card=card,
        )
        return ack if ack else {}

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
        """下载 WeCom 图片/文件。

        注意：WeCom COS URL 已在签名中包含认证，图片返回时已是解密状态。
        aes_key 参数不再用于 AES 解密（URL 签名仅用于访问控制）。
        """
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()
        logger.info(f"[WeComClient] download_file: url_len={len(url)}, data_len={len(data)}")
        return data

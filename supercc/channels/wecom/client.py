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

    # 支持的图片扩展名
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    def _detect_media_type(self, file_path: str) -> str:
        """根据扩展名推断 WeCom media type。"""
        import os
        ext = os.path.splitext(file_path)[1].lower()
        if ext in {".jpg", ".jpeg"}:
            return "image"
        if ext == ".gif":
            return "image"
        if ext == ".webp":
            return "image"
        if ext == ".bmp":
            return "image"
        # 其他默认为 file
        return "file"

    async def upload_media(self, file_path: str) -> str:
        """上传本地文件，返回 media_id（Hermes 3-step 协议）。"""
        import os
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        data = await asyncio.to_thread(open(file_path, "rb").read)
        file_name = os.path.basename(file_path)
        media_type = self._detect_media_type(file_path)
        result = await self._ws._upload_media_bytes(data, media_type, file_name)
        if result.get("errcode") != 0:
            raise RuntimeError(f"upload_media failed: {result.get('errmsg', 'unknown')}")
        return result["media_id"]

    async def send_image(self, chat_id: str, media_id: str) -> str:
        """发送图片消息。"""
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="image",
            image={"media_id": media_id},
        )
        return ack.get("body", {}).get("msgid", "") if ack else ""

    async def send_file(self, chat_id: str, media_id: str) -> str:
        """发送文件消息。"""
        ack = await self._ws.send_message(
            chat_id=chat_id,
            msgtype="file",
            file={"media_id": media_id},
        )
        return ack.get("body", {}).get("msgid", "") if ack else ""

    async def send_image_file(self, chat_id: str, file_path: str) -> str:
        """上传并发送图片（一站式，Hermes send_image_file 方式）。"""
        media_id = await self.upload_media(file_path)
        return await self.send_image(chat_id, media_id)

    async def send_document(self, chat_id: str, file_path: str) -> str:
        """上传并发送文件（一站式，Hermes send_document 方式）。"""
        media_id = await self.upload_media(file_path)
        return await self.send_file(chat_id, media_id)

    # ── 下载 ─────────────────────────────────────────────────────────────────

    async def download_file(self, url: str, aes_key: str = "") -> tuple[bytes, str]:
        """下载并解密 WeCom 图片/文件。

        Returns:
            tuple: (data_bytes, content_disposition_header)
            content_disposition 可用于提取原文件名（如 'attachment; filename="xxx.pdf"'）。
        """
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()
                content_disposition = resp.headers.get("Content-Disposition", "")
        logger.debug(f"[WeComClient] download_file: url_len={len(url)}, data_len={len(data)}, aes_key_present={bool(aes_key)}, cd={content_disposition[:50] if content_disposition else 'None'}")
        if aes_key:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            import base64
            # WeCom doesn't pad base64 keys; add padding if needed
            padded_key = aes_key + '=' * ((4 - len(aes_key) % 4) % 4)
            key = base64.b64decode(padded_key)
            if len(key) != 32:
                logger.warning(f"[WeComClient] Invalid WeCom AES key length: expected 32 bytes, got {len(key)}, skipping decrypt")
                return data, content_disposition
            cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(data) + decryptor.finalize()
            pad_len = decrypted[-1]
            if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
                logger.warning(f"[WeComClient] Invalid PKCS#7 padding: {pad_len}, skipping decrypt")
                return data, content_disposition
            if any(byte != pad_len for byte in decrypted[-pad_len:]):
                logger.warning(f"[WeComClient] PKCS#7 padding mismatch, skipping decrypt")
                return data, content_disposition
            data = decrypted[:-pad_len]
            logger.debug(f"[WeComClient] decrypted to {len(data)} bytes")
        return data, content_disposition

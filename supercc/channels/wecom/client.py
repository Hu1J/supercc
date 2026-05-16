"""企业微信消息发送客户端（基于官方 wecom-aibot-sdk-python）。"""
from __future__ import annotations

import logging

from wecom_aibot_sdk import generate_req_id
from wecom_aibot_sdk.types import (
    SendMarkdownMsgBody,
    SendTemplateCardMsgBody,
    TemplateCard,
)
from wecom_aibot_sdk.types.message import CardTitle, CardButton

logger = logging.getLogger(__name__)


class WeComClient:
    """
    企业微信消息发送客户端。

    封装 wecom-aibot-sdk-python 的 WSClient，提供：
    - send_text / send_markdown：主动发送文本消息
    - send_template_card：发送模板卡片
    - upload_media：上传本地文件（3-step 协议）
    - download_file：下载并解密文件
    """

    def __init__(self, ws_client):
        """
        Args:
            ws_client: WeComWSClient 实例（基于 SDK）
        """
        self._ws = ws_client

    # ── 主动发送 ────────────────────────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str) -> str:
        """发送文本消息（主动发送，无原始帧）。"""
        ack = await self._ws.send_message(
            chatid=chat_id,
            body={"msgtype": "text", "text": {"content": text}},
        )
        return ack.body.get("msgid", "") if ack.body else ""

    async def send_markdown(self, chat_id: str, content: str) -> str:
        """发送 Markdown 消息（主动发送，无原始帧）。"""
        ack = await self._ws.send_message(
            chatid=chat_id,
            body=SendMarkdownMsgBody(markdown={"content": content}),
        )
        return ack.body.get("msgid", "") if ack.body else ""

    async def send_template_card(
        self, chat_id: str, card_type: str, title: str, desc: str = "", buttons: list | None = None
    ) -> str:
        """发送模板卡片（主动发送）。"""
        card = TemplateCard(
            card_type=card_type,
            main_title=CardTitle(title=title, desc=desc) if desc else CardTitle(title=title),
            button_list=[
                CardButton(text=btn["text"], key=btn.get("key", btn["text"]))
                for btn in (buttons or [])
            ],
        )
        ack = await self._ws.send_message(
            chatid=chat_id,
            body=SendTemplateCardMsgBody(template_card=self._build_card(card)),
        )
        return ack.body.get("msgid", "") if ack.body else ""

    # ── 专用业务消息 ─────────────────────────────────────────────────────────

    async def send_authorization_card(self, chat_id: str, reason: str) -> str:
        """发送权限不足引导卡片。"""
        return await self.send_template_card(
            chat_id=chat_id,
            card_type="button_interaction",
            title="权限不足",
            desc=reason,
            buttons=[{"text": "联系管理员", "key": "contact_admin"}],
        )

    async def send_typing_indicator(self, chat_id: str) -> str:
        """发送'正在思考...'提示（text_notice 模板卡片）。"""
        card = TemplateCard(
            card_type="text_notice",
            main_title=CardTitle(title="正在思考...", desc=""),
            source={"desc": "SuperCC"},
        )
        ack = await self._ws.send_message(
            chatid=chat_id,
            body=SendTemplateCardMsgBody(template_card=self._build_card(card)),
        )
        return ack.body.get("msgid", "") if ack.body else ""

    # ── 媒体 ─────────────────────────────────────────────────────────────────

    async def upload_media(self, file_path: str) -> str:
        """上传本地文件，返回 media_id（SDK 3-step 协议）。"""
        result = await self._ws.upload_media(file_path)
        return result.media_id

    async def send_file(self, chat_id: str, media_id: str) -> str:
        """发送文件消息。"""
        ack = await self._ws.send_message(
            chatid=chat_id,
            body={"msgtype": "file", "file": {"media_id": media_id}},
        )
        return ack.body.get("msgid", "") if ack.body else ""

    async def send_image(self, chat_id: str, media_id: str) -> str:
        """发送图片消息。"""
        ack = await self._ws.send_message(
            chatid=chat_id,
            body={"msgtype": "image", "image": {"media_id": media_id}},
        )
        return ack.body.get("msgid", "") if ack.body else ""

    # ── 下载 ─────────────────────────────────────────────────────────────────

    async def download_file(self, url: str, aes_key: str = "") -> bytes:
        """下载并解密文件。"""
        data, _ = await self._ws.download_file(url=url, aes_key=aes_key or None)
        return data

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _build_card(self, card: TemplateCard) -> dict:
        """将 TemplateCard 对象转为 dict（复制 SDK _build_template_card 逻辑）。"""
        result: dict = {"card_type": card.card_type}

        if card.main_title:
            result["main_title"] = {"title": card.main_title.title}
            if card.main_title.desc:
                result["main_title"]["desc"] = card.main_title.desc

        if card.sub_title:
            result["sub_title"] = card.sub_title

        if card.card_action:
            result["card_action"] = {"type": card.card_action.type}
            if card.card_action.url:
                result["card_action"]["url"] = card.card_action.url
            if card.card_action.taskid:
                result["card_action"]["taskid"] = card.card_action.taskid

        if card.button_list:
            result["button_list"] = [
                {"text": btn.text, "key": btn.key}
                for btn in card.button_list
            ]

        if card.task_id:
            result["task_id"] = card.task_id

        if card.source:
            result["source"] = card.source

        if card.card_image:
            result["card_image"] = card.card_image

        if card.horizontal_content_list:
            result["horizontal_content_list"] = card.horizontal_content_list

        if card.jump_list:
            result["jump_list"] = card.jump_list

        return result

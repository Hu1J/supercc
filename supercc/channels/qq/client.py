"""QQ REST API client for outbound message sending.

QQ Official Bot API v2 — 无官方 Python SDK，直接用 aiohttp 调用 REST API。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("qq")

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://api.sgroup.qq.com/oauth2/access_token"
MAX_MESSAGE_LENGTH = 4000


@dataclass
class IncomingQQMessage:
    """Parsed incoming message from QQ WebSocket Gateway."""
    message_id: str
    content: str
    message_type: str       # "C2C_MESSAGE_CREATE" or "GROUP_AT_MESSAGE_CREATE"
    author_id: str          # User openid who sent the message
    author_name: str        # Display name of author
    group_openid: str = ""  # For group messages
    group_name: str = ""    # For group messages
    channel_id: str = ""    # Same as group_openid for groups
    # raw payload
    raw: dict = field(default_factory=dict)


class QQClient:
    """
    QQ 机器人 REST API 客户端（出站消息发送）。

    提供：
    - send_c2c_text: 发送私聊消息
    - send_group_text: 发送群聊消息
    - send_group_markdown: 发送群聊 Markdown 消息
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        bot_openid: str = "",
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_openid = bot_openid
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    def _auth_headers(self) -> dict:
        """Return authorization headers with current access token."""
        if not self._access_token:
            raise RuntimeError("QQ access token not available. Call ensure_token() first.")
        return {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
            "User-Agent": "QQBotAdapter/1.0 (Python)",
        }

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with self._token_lock:
            if self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token

            payload = {
                "grant_type": "client_credentials",
                "appid": self.app_id,
                "secret": self.app_secret,
            }

            async with aiohttp.ClientSession() as sess:
                async with sess.post(TOKEN_URL, data=payload) as resp:
                    data = await resp.json()

            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"QQ Bot token response missing access_token: {data}")

            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info("[QQClient] Access token refreshed, expires in %ds", expires_in)
            return self._access_token

    async def send_c2c_text(
        self,
        user_openid: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a private text message to a user.

        Args:
            user_openid: The user's openid
            text: Message content (max 4000 chars)
            reply_to: Optional message_id to reply to

        Returns:
            The sent message_id
        """
        await self._ensure_token()
        payload: dict = {"content": text}
        if reply_to:
            payload["msg_id"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_BASE}/v2/users/{user_openid}/messages",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("[QQClient] send_c2c_text failed: %s", data)
                return data.get("id", "")

    async def send_group_text(
        self,
        group_openid: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a text message to a group.

        Args:
            group_openid: The group's openid
            text: Message content (max 4000 chars)
            reply_to: Optional message_id to reply to

        Returns:
            The sent message_id
        """
        await self._ensure_token()
        payload: dict = {"content": text}
        if reply_to:
            payload["msg_id"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_BASE}/v2/groups/{group_openid}/messages",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("[QQClient] send_group_text failed: %s", data)
                return data.get("id", "")

    async def send_group_markdown(
        self,
        group_openid: str,
        markdown: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a markdown message to a group (QQ native markdown, msg_type=2).

        Args:
            group_openid: The group's openid
            markdown: Markdown content (QQ supports basic markdown)
            reply_to: Optional message_id to reply to

        Returns:
            The sent message_id
        """
        await self._ensure_token()
        # QQ markdown: msg_type=2 for markdown
        payload: dict = {
            "content": markdown,
            "msg_type": 2,
        }
        if reply_to:
            payload["msg_id"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_BASE}/v2/groups/{group_openid}/messages",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("[QQClient] send_group_markdown failed: %s", data)
                return data.get("id", "")

    async def send_c2c_markdown(
        self,
        user_openid: str,
        markdown: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a markdown message to a user (QQ native markdown, msg_type=2).

        Args:
            user_openid: The user's openid
            markdown: Markdown content
            reply_to: Optional message_id to reply to

        Returns:
            The sent message_id
        """
        await self._ensure_token()
        payload: dict = {
            "content": markdown,
            "msg_type": 2,
        }
        if reply_to:
            payload["msg_id"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_BASE}/v2/users/{user_openid}/messages",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("[QQClient] send_c2c_markdown failed: %s", data)
                return data.get("id", "")

    # ── Media upload (CDN 3-step) ──────────────────────────────────────────────

    async def _upload_media_cdn(
        self,
        file_bytes: bytes,
        file_name: str,
        media_type: int,
    ) -> str:
        """Upload file via QQ CDN (3-step: prepare → put parts → complete).

        Returns the file_id for use in send_*.
        """
        await self._ensure_token()

        # Step 1: Get upload URL
        async with aiohttp.ClientSession() as sess:
            # Prepare upload
            prepare_url = f"{API_BASE}/v2/media/upload"
            prepare_payload = {
                "file_name": file_name,
                "file_size": len(file_bytes),
                "media_type": media_type,
            }
            async with sess.post(
                prepare_url,
                json=prepare_payload,
                headers=self._auth_headers(),
            ) as resp:
                prepare_data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"CDN prepare failed: {prepare_data}")
                upload_url = prepare_data.get("upload_url", "")
                file_id = prepare_data.get("file_id", "")
                upload_headers = prepare_data.get("upload_headers", {})

            if not upload_url or not file_id:
                raise RuntimeError(f"CDN prepare missing upload_url or file_id: {prepare_data}")

            # Step 2: PUT file bytes to upload_url
            upload_headers["Content-Type"] = "application/octet-stream"
            async with sess.put(
                upload_url,
                data=file_bytes,
                headers=upload_headers,
            ) as upload_resp:
                if upload_resp.status not in (200, 201):
                    raise RuntimeError(f"CDN put failed: status={upload_resp.status}")

            # Step 3: Complete upload
            async with sess.post(
                f"{API_BASE}/v2/media/upload",
                json={"file_id": file_id},
                headers=self._auth_headers(),
            ) as complete_resp:
                complete_data = await complete_resp.json()
                if complete_resp.status != 200:
                    raise RuntimeError(f"CDN complete failed: {complete_data}")

            return file_id

    async def send_group_image(
        self,
        group_openid: str,
        image_path: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send an image to a group.

        Args:
            group_openid: The group's openid
            image_path: Path to image file
            reply_to: Optional message_id to reply to

        Returns:
            The sent message_id
        """
        import os

        await self._ensure_token()
        with open(image_path, "rb") as f:
            file_bytes = f.read()

        file_id = await self._upload_media_cdn(
            file_bytes,
            os.path.basename(image_path),
            media_type=1,  # IMAGE
        )

        payload: dict = {
            "content": file_id,
            "msg_type": 7,  # MEDIA
            "media_type": 1,  # IMAGE
        }
        if reply_to:
            payload["msg_id"] = reply_to

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{API_BASE}/v2/groups/{group_openid}/messages",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning("[QQClient] send_group_image failed: %s", data)
                return data.get("id", "")

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_incoming(data: dict) -> IncomingQQMessage | None:
        """Parse a QQ WebSocket event payload into IncomingQQMessage.

        Handles C2C_MESSAGE_CREATE and GROUP_AT_MESSAGE_CREATE events.
        """
        try:
            msg_id = str(data.get("id", ""))
            author = data.get("author", {}) or {}
            author_id = author.get("id", "") or author.get("user_id", "")
            author_name = author.get("username", "") or author.get("nickname", "") or author_id

            content = data.get("content", "")
            # For GROUP_AT_MESSAGE_CREATE, also has a "content" field with the message
            # and potentially "mentions" for @users
            msg_type = data.get("message_type", "")

            group_openid = data.get("group_openid", "") or data.get("guild_id", "")

            return IncomingQQMessage(
                message_id=msg_id,
                content=content,
                message_type=msg_type,
                author_id=author_id,
                author_name=author_name,
                group_openid=group_openid,
                raw=data,
            )
        except Exception as e:
            logger.warning("[QQClient] parse_incoming failed: %s", e)
            return None

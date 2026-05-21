"""微信个人（iLink）消息发送客户端。

支持：
- send_text: 发送文本消息
- send_image: AES-128-ECB CDN 3 步上传发送图片
- send_file/send_video/send_voice: CDN 上传发送媒体文件
- download_media: CDN 下载并解密媒体文件

参考 Hermes Agent: gateway/platforms/weixin.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from supercc.channels.wechat.crypto import (
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    aes_key_to_b64,
    aes_key_to_hex,
    aes_padded_size,
    check_crypto_available,
    generate_aes_key,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 2000

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000

# 消息类型
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

# 媒体类型
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

# Item 类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# Typing 状态
TYPING_START = 1
TYPING_STOP = 2


def _random_wechat_uin() -> str:
    import base64
    import secrets as _secrets
    import struct as _struct
    value = _struct.unpack(">I", _secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: Optional[str], body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _cdn_upload_url(upload_param: str, filekey: str) -> str:
    return (
        f"{ILINK_CDN_BASE_URL}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _cdn_download_url(encrypted_query_param: str) -> str:
    return (
        f"{ILINK_CDN_BASE_URL}/download"
        f"?encrypted_query_param={quote(encrypted_query_param, safe='')}"
    )


def _media_type_str(media_type: int) -> str:
    return {MEDIA_IMAGE: "image", MEDIA_VIDEO: "video", MEDIA_FILE: "file", MEDIA_VOICE: "voice"}.get(media_type, "file")


def _mime_for_media(media_type: int, filename: str = "") -> str:
    if media_type == MEDIA_IMAGE:
        return "image/jpeg"
    if media_type == MEDIA_VIDEO:
        return "video/mp4"
    if media_type == MEDIA_VOICE:
        return "audio/silk"
    import mimetypes
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _item_type_for_media(media_type: int) -> int:
    return {MEDIA_IMAGE: ITEM_IMAGE, MEDIA_VIDEO: ITEM_VIDEO, MEDIA_FILE: ITEM_FILE, MEDIA_VOICE: ITEM_VOICE}.get(media_type, ITEM_FILE)


class WeChatClient:
    """微信个人（iLink）消息发送客户端。

    通过 iLink Bot API 发送文本和媒体消息。
    """

    def __init__(self, token: str, bot_openid: str = "", base_url: str = ILINK_BASE_URL):
        """
        Args:
            token: iLink Bot Token
            bot_openid: 机器人的 openid（用于 from_user_id）
            base_url: iLink API 基础 URL
        """
        self._token = token
        self._bot_openid = bot_openid
        self._base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    def _make_ssl_connector(self):
        try:
            import ssl
            import certifi
        except ImportError:
            return None
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        return aiohttp.TCPConnector(ssl=ssl_ctx)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = self._make_ssl_connector()
            self._session = aiohttp.ClientSession(trust_env=True, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _api_post(self, endpoint: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        """POST 到 iLink API。"""
        session = await self._get_session()
        body = _json_dumps({**payload, "base_info": {"channel_version": "2.2.0"}})
        url = f"{self._base_url}/{endpoint}"
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        headers = _headers(self._token, body)

        async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    async def send_text(
        self,
        openid: str,
        text: str,
        context_token: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> str:
        """发送文本消息（自动分块，超 2000 字符按段落切分）。

        Args:
            openid: 接收者的 openid
            text: 文本内容
            context_token: 上下文 token（用于会话关联）
            client_id: 消息客户端 ID（用于幂等性）

        Returns:
            client_id 字符串
        """
        if not text or not text.strip():
            raise ValueError("send_text: text must not be empty")

        # 按段落分块，每块不超过 MAX_MESSAGE_LENGTH
        chunks = self._chunk_text(text)
        first_chunk = chunks[0]
        rest_chunks = chunks[1:]

        # 第一块带 client_id（幂等），后续块不带
        first_cid = client_id or f"wechat-{uuid.uuid4().hex}"
        resp = await self._send_text_chunk(openid, first_chunk, first_cid, context_token)

        # errcode=-14 → context_token 过期，去掉重试一次
        if resp.get("errcode") == -14 and context_token:
            resp = await self._send_text_chunk(openid, first_chunk, first_cid, None)

        for chunk in rest_chunks:
            await self._send_text_chunk(openid, chunk, None, context_token)

        return str(client_id or resp.get("client_id", ""))

    def _chunk_text(self, text: str) -> list[str]:
        """按段落切分文本，每块不超过 MAX_MESSAGE_LENGTH。"""
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        paragraphs = text.split("\n\n")
        chunks = []
        current = ""
        for para in paragraphs:
            if not para.strip():
                continue
            if len(current) + len(para) + 2 <= MAX_MESSAGE_LENGTH:
                current = (current + "\n\n" + para).strip() if current else para
            else:
                if current:
                    chunks.append(current)
                # 单段超限，按行切
                if len(para) > MAX_MESSAGE_LENGTH:
                    lines = para.split("\n")
                    for line in lines:
                        if len(current) + len(line) + 1 <= MAX_MESSAGE_LENGTH:
                            current = (current + "\n" + line) if current else line
                        else:
                            if current:
                                chunks.append(current)
                            current = line
                    continue
                current = para
        if current:
            chunks.append(current)
        return chunks

    async def _send_text_chunk(
        self,
        openid: str,
        text: str,
        client_id: Optional[str],
        context_token: Optional[str],
    ) -> dict[str, Any]:
        """发送单个文本块。"""
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": openid,
            "client_id": client_id or f"wechat-{uuid.uuid4().hex}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token

        return await self._api_post(
            EP_SEND_MESSAGE,
            {"msg": message},
            API_TIMEOUT_MS,
        )

    async def send_image(
        self,
        openid: str,
        image_path: str,
        context_token: Optional[str] = None,
    ) -> str:
        """发送图片消息（AES-128-ECB CDN 3 步上传）。

        Args:
            openid: 接收者的 openid
            image_path: 本地图片路径
            context_token: 上下文 token

        Returns:
            client_id 字符串
        """
        if not check_crypto_available():
            raise RuntimeError("cryptography package is required for WeChat image send. Install with: pip install cryptography")

        plaintext = Path(image_path).read_bytes()
        filekey = secrets.token_hex(16)
        aes_key = generate_aes_key()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        ciphertext = aes128_ecb_encrypt(plaintext, aes_key)

        # Step 1: 获取上传 URL
        upload_resp = await self._api_post(
            EP_GET_UPLOAD_URL,
            {
                "filekey": filekey,
                "media_type": MEDIA_IMAGE,
                "to_user_id": openid,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": aes_padded_size(rawsize),
                "no_need_thumb": True,
                "aeskey": aes_key_to_hex(aes_key),
            },
            API_TIMEOUT_MS,
        )

        upload_param = str(upload_resp.get("upload_param") or "")
        upload_full_url = str(upload_resp.get("upload_full_url") or "")

        # Step 2: 上传密文到 CDN
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(upload_param, filekey)
        else:
            raise RuntimeError(f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_resp}")

        encrypted_query_param = await self._upload_ciphertext(ciphertext, upload_url)

        # Step 3: 发送消息引用 CDN 上的图片
        aes_key_for_api = aes_key_to_b64(aes_key)
        client_id = f"wechat-{uuid.uuid4().hex}"
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": openid,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [
                {
                    "type": ITEM_IMAGE,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": encrypted_query_param,
                            "aes_key": aes_key_for_api,
                            "encrypt_type": 1,
                        },
                        "mid_size": len(ciphertext),
                    },
                }
            ],
        }
        if context_token:
            message["context_token"] = context_token

        await self._api_post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
        return client_id

    async def _upload_ciphertext(self, ciphertext: bytes, upload_url: str) -> str:
        """上传密文到 CDN。返回 encrypted_query_param。"""
        session = await self._get_session()

        async def _do_upload() -> str:
            async with session.post(
                upload_url,
                data=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    encrypted_param = resp.headers.get("x-encrypted-param")
                    if encrypted_param:
                        await resp.read()
                        return encrypted_param
                    raw = await resp.text()
                    raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
                raw = await resp.text()
                raise RuntimeError(f"CDN upload HTTP {resp.status}: {raw[:200]}")

        return await asyncio.wait_for(_do_upload(), timeout=120)

    async def send_typing(self, openid: str, typing_ticket: str, status: int = TYPING_START) -> None:
        """发送 typing 状态。"""
        await self._api_post(
            EP_SEND_TYPING,
            {
                "ilink_user_id": openid,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            CONFIG_TIMEOUT_MS,
        )

    async def get_config(
        self,
        user_id: str,
        context_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """获取用户配置（包括 typing_ticket）。"""
        payload: dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self._api_post(EP_GET_CONFIG, payload, CONFIG_TIMEOUT_MS)

    async def download_media(
        self,
        encrypted_query_param: str,
        aes_key: Optional[str] = None,
        full_url: Optional[str] = None,
    ) -> bytes:
        """从 CDN 下载并解密媒体文件。

        Args:
            encrypted_query_param: CDN 加密参数
            aes_key: AES-128 密钥（hex 编码），存在时自动解密
            full_url: 直接下载地址（可选）

        Returns:
            解密后的原始媒体字节
        """
        session = await self._get_session()
        if full_url:
            url = full_url
        else:
            url = _cdn_download_url(encrypted_query_param)

        timeout = aiohttp.ClientTimeout(total=120)
        async with session.get(url, headers={"Authorization": f"Bearer {self._token}"}, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"CDN download HTTP {resp.status}")
            data = await resp.read()

        if aes_key:
            key_bytes = bytes.fromhex(aes_key)
            data = aes128_ecb_decrypt(data, key_bytes)

        return data

    async def download_media_to_file(
        self,
        encrypted_query_param: str,
        aes_key: Optional[str] = None,
        full_url: Optional[str] = None,
        suffix: str = "",
    ) -> str:
        """下载媒体文件到临时文件，返回文件路径。"""
        data = await self.download_media(encrypted_query_param, aes_key, full_url)
        fd, path = tempfile.mkstemp(suffix=suffix)
        import os
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    async def send_file(
        self,
        openid: str,
        file_path: str,
        context_token: Optional[str] = None,
    ) -> str:
        """发送文件消息。"""
        return await self._send_media(openid, file_path, MEDIA_FILE, context_token)

    async def send_video(
        self,
        openid: str,
        file_path: str,
        context_token: Optional[str] = None,
    ) -> str:
        """发送视频消息。"""
        return await self._send_media(openid, file_path, MEDIA_VIDEO, context_token)

    async def send_voice(
        self,
        openid: str,
        file_path: str,
        context_token: Optional[str] = None,
    ) -> str:
        """发送语音消息。"""
        return await self._send_media(openid, file_path, MEDIA_VOICE, context_token)

    async def _send_media(
        self,
        openid: str,
        file_path: str,
        media_type: int,
        context_token: Optional[str] = None,
    ) -> str:
        """CDN 上传发送媒体文件（图片/视频/文件/语音）。"""
        if not check_crypto_available():
            raise RuntimeError("cryptography package required for media send. Install with: pip install cryptography")

        plaintext = Path(file_path).read_bytes()
        filekey = secrets.token_hex(16)
        aes_key = generate_aes_key()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        ciphertext = aes128_ecb_encrypt(plaintext, aes_key)

        upload_resp = await self._api_post(
            EP_GET_UPLOAD_URL,
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": openid,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": aes_padded_size(rawsize),
                "no_need_thumb": True,
                "aeskey": aes_key_to_hex(aes_key),
            },
            API_TIMEOUT_MS,
        )

        upload_param = str(upload_resp.get("upload_param") or "")
        upload_full_url = str(upload_resp.get("upload_full_url") or "")

        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(upload_param, filekey)
        else:
            raise RuntimeError(f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_resp}")

        encrypted_query_param = await self._upload_ciphertext(ciphertext, upload_url)

        aes_key_for_api = aes_key_to_b64(aes_key)
        client_id = f"wechat-{uuid.uuid4().hex}"

        media_type_str = _media_type_str(media_type)
        item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": encrypted_query_param,
                "aes_key": aes_key_for_api,
                "encrypt_type": 1,
            },
        }

        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": openid,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": _item_type_for_media(media_type), media_type_str + "_item": item}],
        }
        if context_token:
            message["context_token"] = context_token

        await self._api_post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
        return client_id
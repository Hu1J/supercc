"""微信文件发送 MCP 工具 — 暴露 WeChatSendFile 给 Claude Code 使用。

实现 AES-128-ECB CDN 上传协议（Hermes 方式），
不依赖 WeChat 插件进程。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

import aiohttp

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
API_TIMEOUT_MS = 15_000

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_IMAGE = 2
ITEM_FILE = 4
ITEM_VIDEO = 5

MAX_MESSAGE_LENGTH = 2000
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB

WEIXIN_FILE_GUIDE = """
【微信文件】用户要求发送文件/图片/截图时，调用 mcp__SuperCC__WeChatSendFile(file_paths: list[str])。
"""


def _resolve_path(file_path: str) -> str:
    """将相对路径尝试解析为绝对路径。"""
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
    if os.path.exists(file_path):
        return os.path.abspath(file_path)
    candidates = []
    try:
        from supercc.config import get_config
        cfg = get_config()
        approved = cfg.claude.approved_directory
        if approved:
            candidates.append(os.path.join(approved, file_path))
    except Exception:
        pass
    candidates.append(os.path.join(os.getcwd(), file_path))
    for candidate in candidates:
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return file_path


def _get_wechat_config() -> tuple[str, str]:
    """获取 WeChat token 和 bot_open_id。"""
    from supercc.config import get_config
    cfg = get_config()
    wechat_cfg = getattr(cfg.channels, "wechat", None)
    if not wechat_cfg:
        raise RuntimeError("WeChat channel not configured")
    token = str(wechat_cfg.token or "")
    bot_open_id = str(wechat_cfg.bot_open_id or "")
    if not token:
        raise RuntimeError("WeChat token not configured")
    return token, bot_open_id


def _get_chat_id() -> Optional[str]:
    """从 contextvar 获取当前 chat_id（即用户 openid）。"""
    from supercc.core.message_context import get_current_chat_id
    return get_current_chat_id()


def _random_wechat_uin() -> str:
    import struct
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _cdn_upload_url(upload_param: str, filekey: str) -> str:
    from urllib.parse import quote
    return (
        f"{ILINK_CDN_BASE_URL}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    return cipher.encryptor().update(padded)


def _make_ssl_connector():
    try:
        import ssl, certifi
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


class WeChatSender:
    """在 core 进程中直接发送微信消息，不依赖插件进程。"""

    def __init__(self, token: str, bot_open_id: str = ""):
        self._token = token
        self._bot_open_id = bot_open_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = _make_ssl_connector()
            self._session = aiohttp.ClientSession(trust_env=True, connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _api_post(self, endpoint: str, payload: dict, timeout_ms: int) -> dict:
        session = await self._get_session()
        body = _json_dumps({**payload, "base_info": {"channel_version": "2.2.0"}})
        url = f"{ILINK_BASE_URL}/{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            "Authorization": f"Bearer {self._token}",
        }
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"WeChat API HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    async def _upload_ciphertext(self, ciphertext: bytes, upload_url: str) -> str:
        session = await self._get_session()
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
                raise RuntimeError(f"CDN upload missing x-encrypted-param: {raw[:200]}")
            raw = await resp.text()
            raise RuntimeError(f"CDN upload HTTP {resp.status}: {raw[:200]}")

    async def _send_media(
        self,
        openid: str,
        file_path: str,
        media_type: int,
        item_type: int,
    ) -> str:
        """CDN 上传发送媒体文件。"""
        plaintext = Path(file_path).read_bytes()
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        upload_resp = await self._api_post(
            EP_GET_UPLOAD_URL,
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": openid,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": _aes_padded_size(rawsize),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
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

        from supercc.channels.wechat.crypto import aes_key_to_b64
        aes_key_b64 = aes_key_to_b64(aes_key)
        client_id = f"wechat-{secrets.token_hex(8)}"

        type_str = {ITEM_IMAGE: "image", ITEM_VIDEO: "video", ITEM_FILE: "file"}.get(item_type, "file")
        item: dict = {
            "media": {
                "encrypt_query_param": encrypted_query_param,
                "aes_key": aes_key_b64,
                "encrypt_type": 1,
            },
        }
        if item_type == ITEM_FILE:
            item["file_name"] = Path(file_path).name
            item["len"] = str(rawsize)

        message: dict = {
            "from_user_id": "",
            "to_user_id": openid,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": item_type, f"{type_str}_item": item}],
        }

        await self._api_post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
        return client_id


async def _send_single_file(file_path: str, openid: str) -> str:
    """发送单个文件/图片，返回 client_id。"""
    token, _ = _get_wechat_config()
    sender = WeChatSender(token)
    try:
        ext = Path(file_path).suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTS:
            return await sender._send_media(openid, file_path, MEDIA_IMAGE, ITEM_IMAGE)
        elif ext == ".mp4" or ext in {".mov", ".avi", ".mkv", ".webm", ".3gp"}:
            return await sender._send_media(openid, file_path, MEDIA_VIDEO, ITEM_VIDEO)
        elif ext in {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".silk"}:
            return await sender._send_media(openid, file_path, MEDIA_VOICE, ITEM_FILE)
        else:
            return await sender._send_media(openid, file_path, MEDIA_FILE, ITEM_FILE)
    finally:
        await sender.close()


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "WeChatSendFile",
    "发送本地文件或图片到微信用户（通过当前活跃会话的 openid）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def wechat_send_file(args: dict) -> dict:
    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}
    if not isinstance(file_paths, list):
        return {"content": [{"type": "text", "text": "file_paths 必须是列表"}], "is_error": True}

    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃微信会话，请先在微信里发一条消息"}],
            "is_error": True,
        }

    # 验证文件
    errors = []
    for fp in file_paths:
        resolved = _resolve_path(fp)
        if not os.path.exists(resolved):
            errors.append(f"文件不存在: {fp}")
        elif os.path.getsize(resolved) > MAX_FILE_SIZE:
            errors.append(f"{os.path.basename(fp)} 超过 30MB 限制")
    if errors:
        return {"content": [{"type": "text", "text": "\n".join(errors)}], "is_error": True}

    # 并发发送
    async def send_one(fp: str) -> tuple[str, str | None]:
        try:
            await _send_single_file(fp, chat_id)
            return (fp, None)
        except Exception as e:
            return (fp, str(e))

    results = await asyncio.gather(*[send_one(fp) for fp in file_paths])

    sent, failed = [], []
    for fp, err in results:
        if err:
            failed.append(f"{os.path.basename(fp)}: {err}")
        else:
            sent.append(os.path.basename(fp))

    if not failed:
        msg = f"已发送: {', '.join(sent)}"
        return {"content": [{"type": "text", "text": msg}]}
    else:
        parts = []
        if sent:
            parts.append(f"已发送: {', '.join(sent)}")
        parts.append(f"失败: {', '.join(failed)}")
        return {"content": [{"type": "text", "text": "\n".join(parts)}], "is_error": bool(not sent)}

"""WeCom 文件发送 MCP 工具 — 暴露 WeComSendFile 给 Claude Code 使用。

使用 aiohttp 直接实现 3-step 上传协议（Hermes 方式），
不依赖 wecom-aibot-sdk，避免 SDK 连接管理问题。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from typing import Optional

import aiohttp

from claude_agent_sdk import tool

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB
UPLOAD_CHUNK_SIZE = 1024 * 1024   # 1MB chunks
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# SSL verification can be disabled for development with self-signed certs
_WS_SSL_VERIFY = True

WECOM_FILE_GUIDE = """
【企业微信文件】用户要求发送文件/图片/截图时，调用 mcp__SuperCC__WeComSendFile(file_paths: list[str])。
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


def _get_wecom_credentials() -> tuple[str, str]:
    """从 config 获取 WeCom bot_id 和 secret。"""
    from supercc.config import get_config
    cfg = get_config()
    wecom_cfg = cfg.channels.wecom
    bot_id = wecom_cfg.bot_id or wecom_cfg.agent_id
    secret = wecom_cfg.secret or wecom_cfg.corp_secret
    return bot_id, secret


def _get_chat_id() -> Optional[str]:
    """从 contextvar 获取当前 chat_id。"""
    from supercc.core.claude.message_context import get_current_chat_id
    return get_current_chat_id()


def _detect_media_type(file_path: str) -> str:
    """根据扩展名推断 WeCom media type。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "image"
    return "file"


class WeComUploader:
    """WeCom 文件上传器 — aiohttp 直连，3-step WS 协议（Hermes 方式）。"""

    def __init__(self, bot_id: str, bot_secret: str):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._device_id = uuid.uuid4().hex

    async def __aenter__(self):
        try:
            await self._connect()
        except BaseException:
            await self.disconnect()
            raise
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    async def _connect(self):
        """连接 WeCom WS 并认证。"""
        import ssl as ssl_module

        ssl_ctx = None
        if not _WS_SSL_VERIFY:
            ssl_ctx = ssl_module.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl_module.CERT_NONE

        self._session = aiohttp.ClientSession()
        # Compat: some aiohttp versions don't have ClientWSTimeout(total=...)
        try:
            ws_timeout = aiohttp.ClientWSTimeout(total=30)
        except (AttributeError, TypeError):
            ws_timeout = 30.0
        self._ws = await self._session.ws_connect(
            DEFAULT_WS_URL,
            protocols=["wss"],
            timeout=ws_timeout,
            ssl=ssl_ctx,
        )

        # 1. 发送 subscribe 认证
        req_id = uuid.uuid4().hex
        await self._ws.send_json({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.bot_secret,
                "device_id": self._device_id,
            },
        })

        # 2. 等待认证响应
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                cmd = data.get("cmd", "")
                resp_req_id = data.get("headers", {}).get("req_id", "")
                if cmd == "pong":
                    continue
                if resp_req_id == req_id:
                    errcode = data.get("errcode", -1)
                    if errcode != 0:
                        raise RuntimeError(f"WeCom subscribe failed: errcode={errcode}, errmsg={data.get('errmsg', 'unknown')}")
                    break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError(f"WeCom WS error: {msg.data}")
        else:
            raise RuntimeError("WeCom subscribe timed out")

    async def disconnect(self):
        """断开连接（幂等，异常不扩散）。"""
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._session:
                await self._session.close()
        except Exception:
            pass
        self._ws = None
        self._session = None

    async def _send_request(self, cmd: str, body: dict, timeout: float = 8.0) -> dict:
        """发送请求并等待关联响应。"""
        req_id = uuid.uuid4().hex
        future = asyncio.get_event_loop().create_future()

        async def listener():
            try:
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        resp_req_id = data.get("headers", {}).get("req_id", "")
                        if resp_req_id == req_id:
                            future.set_result(data)
                            return
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        future.set_result({"errcode": -1, "errmsg": f"WS error: {msg.data}"})
                        return
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if not future.done():
                    future.set_result({"errcode": -1, "errmsg": f"listener error: {e}"})

        listener_task = asyncio.create_task(listener())

        await self._ws.send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return {"errcode": -1, "errmsg": f"{cmd} timeout ({timeout}s)"}
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    async def _upload_bytes(
        self, data: bytes, media_type: str, file_name: str
    ) -> str:
        """3-step 上传协议，返回 media_id。"""
        total_size = len(data)
        total_chunks = (total_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE

        # Step 1: init
        init_resp = await self._send_request(
            "aibot_upload_media_init",
            {
                "type": media_type,
                "filename": file_name,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "md5": hashlib.md5(data).hexdigest(),
            },
            timeout=10.0,
        )
        if init_resp.get("errcode", -1) != 0:
            raise RuntimeError(f"upload_media_init failed: {init_resp.get('errmsg')}")
        upload_id = str(init_resp.get("body", {}).get("upload_id", "")).strip()
        if not upload_id:
            raise RuntimeError("upload_media_init returned no upload_id")

        # Step 2: chunks
        for idx in range(total_chunks):
            start = idx * UPLOAD_CHUNK_SIZE
            chunk = data[start : start + UPLOAD_CHUNK_SIZE]
            chunk_resp = await self._send_request(
                "aibot_upload_media_chunk",
                {
                    "upload_id": upload_id,
                    "chunk_index": idx,
                    "total_chunks": total_chunks,
                    "base64_data": base64.b64encode(chunk).decode("ascii"),
                },
                timeout=10.0,
            )
            if chunk_resp.get("errcode", -1) != 0:
                raise RuntimeError(f"upload_media_chunk {idx} failed: {chunk_resp.get('errmsg')}")

        # Step 3: finish
        finish_resp = await self._send_request(
            "aibot_upload_media_finish",
            {"upload_id": upload_id},
            timeout=10.0,
        )
        if finish_resp.get("errcode", -1) != 0:
            raise RuntimeError(f"upload_media_finish failed: {finish_resp.get('errmsg')}")
        media_id = str(finish_resp.get("body", {}).get("media_id", "")).strip()
        if not media_id:
            raise RuntimeError("upload_media_finish returned no media_id")

        return media_id

    async def _do_send(self, chat_id: str, media_type: str, media_id: str):
        """发送图片/文件消息。"""
        resp = await self._send_request(
            "aibot_send_msg",
            {
                "chatid": chat_id,
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
            timeout=8.0,
        )
        if resp.get("errcode", -1) != 0:
            raise RuntimeError(f"send_msg failed: errcode={resp.get('errcode')}, errmsg={resp.get('errmsg')}")
        return resp.get("body", {}).get("msgid", "")

    async def send_image_file(self, chat_id: str, file_path: str) -> str:
        """上传图片文件并发送。"""
        resolved = _resolve_path(file_path)
        data = await asyncio.to_thread(lambda: open(resolved, "rb").read())
        file_name = os.path.basename(resolved)
        media_type = _detect_media_type(file_path)
        media_id = await self._upload_bytes(data, media_type, file_name)
        return await self._do_send(chat_id, media_type, media_id)

    async def send_document(self, chat_id: str, file_path: str) -> str:
        """上传普通文件并发送。"""
        resolved = _resolve_path(file_path)
        data = await asyncio.to_thread(lambda: open(resolved, "rb").read())
        file_name = os.path.basename(resolved)
        media_id = await self._upload_bytes(data, "file", file_name)
        return await self._do_send(chat_id, "file", media_id)


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件，返回 msg_id。"""
    bot_id, secret = _get_wecom_credentials()
    resolved = _resolve_path(file_path)
    ext = os.path.splitext(resolved)[1].lower()
    logger.debug("bot_id=%s..., chat_id=%s, file=%s, ext=%s",
                 bot_id[:8], chat_id, os.path.basename(resolved), ext)

    async with WeComUploader(bot_id, secret) as uploader:
        if ext in SUPPORTED_IMAGE_EXTS:
            msg_id = await uploader.send_image_file(chat_id, resolved)
        else:
            msg_id = await uploader.send_document(chat_id, resolved)
    logger.debug("sent successfully: msg_id=%s, file=%s", msg_id, os.path.basename(resolved))
    return msg_id


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "WeComSendFile",
    "发送本地文件或图片到企业微信用户（通过当前活跃会话的 chat_id）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def wecom_send_file(args: dict) -> dict:
    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}
    if not isinstance(file_paths, list):
        return {"content": [{"type": "text", "text": "file_paths 必须是列表"}], "is_error": True}

    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃企业微信会话，请先在企业微信里发一条消息"}],
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

    ok = [fp for fp, err in results if err is None]
    fail = [(fp, err) for fp, err in results if err is not None]

    lines = []
    if ok:
        lines.append(f"✅ 已发送 {len(ok)} 个文件")
        for fp in ok:
            lines.append(f"  • {os.path.basename(fp)}")
    if fail:
        lines.append(f"❌ 失败 {len(fail)} 个")
        for fp, err in fail:
            lines.append(f"  • {os.path.basename(fp)}: {err}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}

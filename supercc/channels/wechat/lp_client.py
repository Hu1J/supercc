"""微信个人（iLink）Long Polling 入站客户端。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_UPDATES = "ilink/bot/getupdates"
LONG_POLL_TIMEOUT_MS = 35_000
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14


def _random_wechat_uin() -> str:
    import base64
    import secrets
    import struct
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: Optional[str], body: str) -> dict[str, str]:
    import re
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


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


def _make_ssl_connector():
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _is_stale_session_ret(ret: Optional[int], errcode: Optional[int], errmsg: Optional[str]) -> bool:
    """True when iLink returns ret=-2 / errcode=-2 with 'unknown error'."""
    if ret != -2 and errcode != -2:
        return False
    return (errmsg or "").lower() == "unknown error"


class WeChatLongPollingClient:
    """iLink Bot API Long Polling 入站客户端。

    通过 HTTP Long Polling 接收微信消息：
    POST https://ilinkai.weixin.qq.com/ilink/bot/getupdates
    body: {"get_updates_buf": sync_buf}
    Header: Authorization: Bearer {token}
    timeout: 40s
    返回 messages + sync_buf
    """

    POLL_URL = f"{ILINK_BASE_URL}/{EP_GET_UPDATES}"
    SYNC_BUF_FILE = "wechat.sync.buf"

    def __init__(self, token: str, sync_buf: str = "", data_dir: str = ""):
        self._token = token
        self._sync_buf = sync_buf
        self._data_dir = data_dir
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    def _sync_buf_path(self) -> Path:
        return Path(self._data_dir) / self.SYNC_BUF_FILE if self._data_dir else Path(self._data_dir or ".") / self.SYNC_BUF_FILE

    def load_sync_buf(self) -> str:
        """从磁盘加载 sync_buf。"""
        path = self._sync_buf_path()
        if not path.exists():
            return ""
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
        except Exception:
            return ""

    def save_sync_buf(self, sync_buf: str) -> None:
        """保存 sync_buf 到磁盘。"""
        if not self._data_dir:
            return
        path = self._sync_buf_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"get_updates_buf": sync_buf}), encoding="utf-8")
        except Exception as exc:
            logger.warning("wechat: failed to save sync_buf: %s", exc)

    async def _api_post(self, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        """POST 到 iLink API。"""
        if self._session is None or self._session.closed:
            connector = _make_ssl_connector()
            self._session = aiohttp.ClientSession(trust_env=True, connector=connector)

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = _headers(self._token, body)
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)

        async with self._session.post(
            self.POLL_URL,
            data=body,
            headers=headers,
            timeout=timeout,
        ) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink POST getupdates HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)

    async def poll(self) -> list[dict[str, Any]]:
        """执行一次 Long Polling 请求。返回消息列表。"""
        try:
            resp = await self._api_post(
                {"get_updates_buf": self._sync_buf},
                timeout_ms=LONG_POLL_TIMEOUT_MS + 5000,  # 额外5s缓冲
            )

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            errmsg = resp.get("errmsg", "")

            if ret not in {0, None} or errcode not in {0, None}:
                if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE or _is_stale_session_ret(ret, errcode, errmsg):
                    logger.error("wechat: session expired; pausing for 10 minutes")
                    await asyncio.sleep(600)
                    return []
                logger.warning(
                    "wechat: getUpdates failed ret=%s errcode=%s errmsg=%s",
                    ret, errcode, errmsg,
                )
                return []

            new_sync_buf = str(resp.get("get_updates_buf") or "")
            if new_sync_buf:
                self._sync_buf = new_sync_buf
                self.save_sync_buf(new_sync_buf)

            return resp.get("msgs") or []

        except asyncio.TimeoutError:
            # 超时是正常行为（long poll 等待新消息）
            return []
        except Exception as exc:
            logger.error("wechat: poll error: %s", exc)
            raise

    async def run_loop(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        """持续轮询消息，通过 on_message 回调处理。

        Args:
            on_message: 回调函数，接收原始消息字典。
        """
        self._running = True
        # 从磁盘恢复 sync_buf
        self._sync_buf = self.load_sync_buf()
        consecutive_failures = 0

        while self._running:
            try:
                msgs = await self.poll()
                for msg in msgs:
                    try:
                        await on_message(msg)
                    except Exception as exc:
                        logger.error("wechat: on_message error for msg from=%s: %s", _safe_id(msg.get("from_user_id")), exc)

                consecutive_failures = 0

                # long poll 返回空是正常情况，继续轮询
                if not msgs:
                    await asyncio.sleep(0.1)

            except asyncio.TimeoutError:
                consecutive_failures += 1
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            except Exception as exc:
                consecutive_failures += 1
                logger.error("wechat: run_loop error (%d/%d): %s", consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                wait = BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS
                await asyncio.sleep(wait)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    async def close(self) -> None:
        """停止轮询并关闭 session。"""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
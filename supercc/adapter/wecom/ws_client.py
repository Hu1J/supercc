"""WeCom WebSocket 长连接客户端（AI Bot 模式）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 长连接客户端。

    连接地址: wss://openws.work.weixin.qq.com
    认证: botId + secret（URL query params）
    """

    WS_URL = "wss://openws.work.weixin.qq.com"

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        on_message: MessageCallback | None = None,
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._on_message = on_message
        self._ws: Any = None
        self._running = False

    def _make_sign(self, timestamp: str) -> str:
        """生成签名。"""
        s = f"{self.bot_id}{timestamp}{self.bot_secret}"
        return hashlib.sha256(s.encode()).hexdigest()

    async def connect(self):
        """建立 WebSocket 连接。"""
        import aiohttp
        timestamp = str(int(time.time()))
        sign = self._make_sign(timestamp)
        params = f"botId={self.bot_id}&timestamp={timestamp}&sign={sign}"
        url = f"{self.WS_URL}?{params}"

        session = aiohttp.ClientSession()
        self._ws = await session.ws_connect(url, autoping=False)
        self._running = True
        logger.info("[WeComWS] Connected")
        asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """持续读取服务器消息。"""
        import aiohttp
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_message(data)
                except Exception:
                    logger.exception("[WeComWS] Error handling message")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[WeComWS] WS error: {self._ws.exception()}")
                break

    async def _handle_message(self, data: dict):
        """处理入站消息。"""
        msg_type = data.get("msgtype", "")
        if msg_type in ("text", "image", "file", "voice"):
            if self._on_message:
                await self._on_message(data)

    async def send(self, payload: dict) -> None:
        """发送消息到服务器。"""
        if self._ws:
            await self._ws.send_str(json.dumps(payload))

    async def close(self):
        """关闭连接。"""
        self._running = False
        if self._ws:
            await self._ws.close()

    def start(self):
        """启动连接（非阻塞，供外部调用）。"""
        asyncio.ensure_future(self.connect())

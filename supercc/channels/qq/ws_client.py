"""QQ WebSocket Gateway client (aiohttp 直连实现，无官方 Python SDK）。

QQ Official Bot API v2 连接流程：
1. POST /oauth2/access_token — 获取 access_token
2. GET /gateway — 获取 WebSocket gateway URL
3. WS 连接 gateway，发送 identify 帧
4. 监听事件：C2C_MESSAGE_CREATE、GROUP_AT_MESSAGE_CREATE 等
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Awaitable, Optional

import aiohttp

logger = logging.getLogger("qq")

MessageCallback = Callable[[dict], Awaitable[None]]

DEFAULT_WS_URL = "wss://api.sgroup.qq.com/"
CONNECT_TIMEOUT_SECONDS = 20.0
HEARTBEAT_INTERVAL_SECONDS = 30.0

# QQ Bot API endpoints
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://api.sgroup.qq.com/oauth2/access_token"
GATEWAY_URL_PATH = "/gateway"

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]


class QQWSClient:
    """
    QQ 机器人 WebSocket Gateway 客户端。

    直接与 QQ Bot WebSocket 网关交互，手动实现协议：
    - access_token 获取
    - gateway URL 发现
    - identify 认证
    - 事件分发（C2C_MESSAGE_CREATE、GROUP_AT_MESSAGE_CREATE）
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: MessageCallback | None = None,
        ws_url: str = DEFAULT_WS_URL,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self._on_message = on_message
        self._ws_url = ws_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Any = None
        self._running = False
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._device_id = uuid.uuid4().hex
        self._sequence: int = 0
        self._op_code: int = 0  # WebSocket opcode from server

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ── Token management ──────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with self._token_lock:
            # Double-check after acquiring lock
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
            logger.info("[QQWS] Access token refreshed, expires in %ds", expires_in)
            return self._access_token

    async def _get_gateway_url(self) -> str:
        """Fetch the WebSocket gateway URL from the REST API."""
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "User-Agent": "QQBotAdapter/1.0 (Python)",
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.get(f"{API_BASE}{GATEWAY_URL_PATH}", headers=headers) as resp:
                data = await resp.json()

        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ Bot gateway response missing url: {data}")
        return url

    # ── WebSocket lifecycle ────────────────────────────────────────────────────

    async def _open_connection(self) -> None:
        """Open and authenticate websocket connection."""
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        gateway_url = await self._get_gateway_url()
        logger.info("[QQWS] Gateway URL: %s", gateway_url)

        self._ws = await self._session.ws_connect(
            gateway_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        logger.info("[QQWS] WebSocket connected")

        # Send identify
        token = await self._ensure_token()
        await self._send_json({
            "op": 2,  # Identify
            "d": {
                "token": f"QQBot {token}",
                "intents": 1 << 30,  # Guild Messages (C2C + Group)
                "shard": [0, 1],
                "properties": {
                    "$os": "python",
                    "$browser": "supercc",
                    "$device": "supercc",
                },
            },
        })

    async def _cleanup_ws(self) -> None:
        """Close websocket and session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _send_json(self, payload: dict) -> None:
        """Send JSON frame over websocket."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("QQ websocket is not connected")
        await self._ws.send_json(payload)

    async def connect(self) -> None:
        """Connect to QQ WS gateway (async, called from main loop)."""
        await self._open_connection()
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("[QQWS] Connected to QQ WS gateway")

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors."""
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[QQWS] WebSocket error: %s", exc)

                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)

                try:
                    await self._open_connection()
                    backoff_idx = 0
                    logger.info("[QQWS] Reconnected")
                except Exception as reconnect_exc:
                    logger.warning("[QQWS] Reconnect failed: %s", reconnect_exc)

    async def _read_events(self) -> None:
        """Read websocket frames until connection closes."""
        if not self._ws:
            raise RuntimeError("QQ websocket not connected")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.debug("[QQWS] Failed to parse payload: %r", msg.data)
                    continue
                await self._dispatch_payload(payload)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                logger.debug("[QQWS] Binary frame ignored")
            elif msg.type in {aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG}:
                pass  # aiohttp handles ping/pong automatically
            elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("QQ websocket closed")
            else:
                logger.debug("[QQWS] Unknown frame type: %s", msg.type)

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats to keep connection alive."""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    # Discord-like heartbeat: send opcode 1 heartbeat with sequence
                    await self._send_json({
                        "op": 1,
                        "d": self._sequence,
                    })
                except Exception as exc:
                    logger.debug("[QQWS] Heartbeat failed: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _dispatch_payload(self, payload: dict) -> None:
        """Route inbound websocket payloads."""
        op = payload.get("op", 0)
        self._op_code = payload.get("op", 0)

        # Heartbeat ACK
        if op == 11:
            logger.debug("[QQWS] Heartbeat ACK received")
            return

        # Hello — start heartbeats and resume
        if op == 10:
            logger.info("[QQWS] Hello received, starting heartbeats")
            interval = payload.get("d", {}).get("heartbeat_interval", HEARTBEAT_INTERVAL_SECONDS * 1000)
            # Update heartbeat interval if server suggests one
            if interval > 0:
                # We use a simple sleep loop, interval is in ms
                pass
            return

        # Reconnect — token may be invalid
        if op == 7:
            logger.warning("[QQWS] Received reconnect opcode (7), invalidating token")
            self._access_token = None
            self._token_expires_at = 0.0
            return

        # Invalid session
        if op == 9:
            logger.warning("[QQWS] Invalid session, clearing token and reconnecting")
            self._access_token = None
            return

        # Dispatch event
        if op == 0:
            self._sequence = payload.get("s", 0) or self._sequence
            event_data = payload.get("d", {})
            event_name = payload.get("t", "")

            logger.debug("[QQWS] Event: %s", event_name)

            if event_name in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                if self._on_message:
                    await self._on_message(event_data)
            elif event_name == "INTERACTION_CREATE":
                # Button click events — handle if callback is set
                if self._on_message:
                    await self._on_message({"_interaction": True, **event_data})
            elif event_name:
                logger.debug("[QQWS] Unhandled event type: %s", event_name)
        else:
            logger.debug("[QQWS] Unknown opcode: op=%d", op)

    async def close(self) -> None:
        """Disconnect from QQ gateway."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._access_token = None
        self._token_expires_at = 0.0
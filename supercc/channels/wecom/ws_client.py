"""企业微信 WebSocket 客户端 - aiohttp 直连实现。

根因：官方 wecom-aibot-sdk 的 WSClientOptions 缺少 device_id 字段，
导致 subscribe body 不带 device_id，WeCom 服务器无法路由消息到该连接。

参照 Hermes gateway/platforms/wecom.py 的 aiohttp 实现。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any, Callable, Awaitable

import aiohttp

logger: Any = __import__("logging").getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 10
APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_PING = "ping"


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 客户端（aiohttp 直连实现）。

    对外接口与原 SDK 版本保持一致：
    start / close / is_connected / get_reply_req_id / pop_reply_req_id /
    reply_stream / reply_text / send_markdown / send_text / send_message
    """

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        on_message: MessageCallback | None = None,
        ws_url: str = DEFAULT_WS_URL,
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._on_message: MessageCallback | None = on_message
        self._ws_url = ws_url

        # aiohttp session and WebSocket
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._device_id = uuid.uuid4().hex

        # Connection state
        self._running = False
        self._connected = False
        self._authenticated = False
        self._reconnect_delay = 1.0

        # Daemon thread state
        self._thread: threading.Thread | None = None
        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: threading.Event | None = None

        # Reply req_id storage: msgid -> req_id
        self._reply_req_ids: dict[str, str] = {}
        self._reply_lock = threading.Lock()

        # Pending requests for correlated responses
        self._pending_responses: dict[str, asyncio.Future] = {}

    # ── 内部工具 ───────────────────────────────────────────────────────

    def _new_req_id(self, prefix: str = "req") -> str:
        return f"{prefix}_{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}"

    def _payload_req_id(self, payload: dict) -> str:
        headers = payload.get("headers") or {}
        return headers.get("req_id", "") or payload.get("req_id", "")

    async def _send_json(self, data: dict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        await self._ws.send_json(data)

    async def _recv_json(self) -> dict | None:
        if self._ws is None:
            return None
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return json.loads(msg.data)
        return None

    # ── 连接与认证 ───────────────────────────────────────────────────

    async def connect(self) -> None:
        """Async connect — called within daemon thread's event loop."""
        await self._open_connection()
        self._running = True
        self._reconnect_delay = 1.0

        # Start background tasks
        listen_task = asyncio.create_task(self._listen_loop())
        ping_task = asyncio.create_task(self._ping_loop())

        try:
            await asyncio.gather(listen_task, ping_task)
        finally:
            self._running = False

    async def _open_connection(self) -> None:
        """Open and authenticate a websocket connection."""
        self._authenticated = False
        self._connected = False

        # Close old session
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._ws = None

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        # Subscribe with device_id (关键：device_id 必须发送，否则服务器无法路由消息)
        req_id = self._new_req_id("subscribe")
        await self._send_json({
            "cmd": APP_CMD_SUBSCRIBE,
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.bot_secret,
                "device_id": self._device_id,
            },
        })

        # Wait for auth ack
        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in {0, None}:
            raise RuntimeError(f"WeCom auth failed: {auth_payload.get('errmsg')} (errcode={errcode})")

        self._authenticated = True
        self._connected = True
        logger.info("[WeComWS] Authenticated successfully, device_id=%s", self._device_id)

    async def _wait_for_handshake(self, req_id: str) -> dict:
        """Wait for subscribe acknowledgement."""
        if not self._ws:
            raise RuntimeError("WebSocket not initialized")

        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")

            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                if not payload:
                    continue
                cmd = payload.get("cmd", "")
                # Ignore pings during handshake
                if cmd == APP_CMD_PING:
                    continue
                # Return if this is our auth response
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("[WeComWS] Ignoring pre-auth payload: %s", cmd)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed during authentication")

    # ── 消息循环 ─────────────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors."""
        while self._running and self._ws is not None:
            try:
                msg = await self._ws.receive()
            except Exception as e:
                logger.warning(f"[WeComWS] receive error: {e}, reconnecting...")
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue

                cmd = payload.get("cmd", "")
                req_id = self._payload_req_id(payload)

                # Correlated response
                if req_id and req_id in self._pending_responses:
                    future = self._pending_responses.pop(req_id)
                    if not future.done():
                        future.set_result(payload)
                    continue

                # Ping
                if cmd == APP_CMD_PING:
                    await self._send_json({"cmd": APP_CMD_PING, "headers": {"req_id": req_id or self._new_req_id("pong")}})
                    continue

                # Dispatch to message handler
                if cmd in (APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK, APP_CMD_EVENT_CALLBACK):
                    await self._dispatch_payload(payload)

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("[WeComWS] WebSocket error")
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                logger.warning("[WeComWS] WebSocket closed")
                break

        # Reconnect
        if self._running:
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        if not self._running:
            return
        logger.info(f"[WeComWS] Reconnecting in {self._reconnect_delay}s...")
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)
        try:
            await self._open_connection()
            self._reconnect_delay = 1.0
        except Exception as e:
            logger.warning(f"[WeComWS] Reconnect failed: {e}")

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep connection alive."""
        while self._running and self._ws is not None:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if not self._running or self._ws is None:
                break
            try:
                await self._send_json({
                    "cmd": APP_CMD_PING,
                    "headers": {"req_id": self._new_req_id("ping")},
                })
            except Exception as e:
                logger.warning(f"[WeComWS] Ping failed: {e}")
                break

    async def _dispatch_payload(self, payload: dict) -> None:
        """Parse and forward message to business callback."""
        body = payload.get("body") or {}
        cmd = payload.get("cmd", "")
        headers = payload.get("headers") or {}
        req_id = headers.get("req_id", "") or ""

        msg_id = str(body.get("msgid") or "")
        logger.info(f"[WeComWS] ★ received cmd={cmd}, msgid={msg_id[:20] if msg_id else 'None'}, req_id={req_id[:20] if req_id else 'None'}, body_keys={list(body.keys())}")

        # Store msgid → req_id mapping for reply
        if msg_id and req_id:
            with self._reply_lock:
                self._reply_req_ids[msg_id] = req_id
            logger.info(f"[WeComWS] stored _reply_req_ids[{msg_id[:20]}] = {req_id[:20] if req_id else 'None'}")

        if self._on_message:
            await self._on_message(body)

    # ── Outbound API ─────────────────────────────────────────────────

    async def _send_request(self, cmd: str, body: dict, timeout: float = 20.0) -> dict:
        """Send request and wait for correlated response."""
        req_id = self._new_req_id(cmd)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_responses[req_id] = future

        await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_responses.pop(req_id, None)
            raise TimeoutError(f"WeCom {cmd} timeout ({timeout}s)")

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        """Proactively send markdown message (fire-and-forget)."""
        try:
            await self._send_json({
                "cmd": APP_CMD_SEND,
                "headers": {"req_id": self._new_req_id("send")},
                "body": {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": content[:4000]}},
            })
            return {"errcode": 0, "errmsg": ""}
        except Exception as e:
            return {"errcode": -1, "errmsg": str(e)}

    async def send_text(self, chat_id: str, content: str) -> dict:
        """Proactively send text message (fire-and-forget)."""
        try:
            await self._send_json({
                "cmd": APP_CMD_SEND,
                "headers": {"req_id": self._new_req_id("send")},
                "body": {"chatid": chat_id, "msgtype": "text", "text": {"content": content[:4000]}},
            })
            return {"errcode": 0, "errmsg": ""}
        except Exception as e:
            return {"errcode": -1, "errmsg": str(e)}

    async def reply_text(self, reply_req_id: str, content: str) -> dict:
        """Reply using the inbound req_id directly (Hermes pattern).

        关键：必须用原始 inbound req_id，不能生成新 req_id。
        WeCom 回复时用原始 req_id 做关联，新 req_id 永远等不到。
        """
        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            return {"errcode": -1, "errmsg": "reply_req_id is required"}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_responses[normalized_req_id] = future

        try:
            await self._send_json({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": normalized_req_id},
                "body": {"msgtype": "markdown", "markdown": {"content": content[:4000]}},
            })
            response = await asyncio.wait_for(future, timeout=20.0)
            return {"errcode": response.get("errcode", 0), "errmsg": response.get("errmsg", "")}
        except asyncio.TimeoutError:
            self._pending_responses.pop(normalized_req_id, None)
            return {"errcode": -1, "errmsg": "WeCom aibot_respond_msg timeout (20s)"}
        except Exception as e:
            self._pending_responses.pop(normalized_req_id, None)
            return {"errcode": -1, "errmsg": str(e)}

    async def reply_stream(
        self,
        reply_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
    ) -> dict:
        """Send streaming reply (markdown) using inbound req_id directly."""
        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            return {"errcode": -1, "errmsg": "reply_req_id is required"}

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_responses[normalized_req_id] = future

        try:
            await self._send_json({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": normalized_req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "content": content[:4000],
                        "finish": finish,
                    },
                },
            })
            response = await asyncio.wait_for(future, timeout=20.0)
            return {"errcode": response.get("errcode", 0), "errmsg": response.get("errmsg", "")}
        except asyncio.TimeoutError:
            self._pending_responses.pop(normalized_req_id, None)
            return {"errcode": -1, "errmsg": "WeCom aibot_respond_msg timeout (20s)"}
        except Exception as e:
            self._pending_responses.pop(normalized_req_id, None)
            return {"errcode": -1, "errmsg": str(e)}

    async def send_message(
        self,
        chat_id: str,
        msgtype: str,
        content: str | None = None,
        markdown: dict | None = None,
        text: dict | None = None,
        template_card: dict | None = None,
        file: dict | None = None,
        image: dict | None = None,
    ) -> dict:
        """General proactive send (fire-and-forget, no correlation wait).

        WeCom's aibot_send_msg doesn't send a correlated response frame —
        the ack comes via aibot_msg_callback which goes through on_message.
        Using _send_request correlation would timeout after 10s.
        """
        body: dict[str, Any] = {"chatid": chat_id, "msgtype": msgtype}
        if markdown is not None:
            body["markdown"] = markdown
        if text is not None:
            body["text"] = text
        if template_card is not None:
            body["template_card"] = template_card
        if file is not None:
            body["file"] = file
        if image is not None:
            body["image"] = image

        try:
            await self._send_json({"cmd": APP_CMD_SEND, "headers": {"req_id": self._new_req_id("send")}, "body": body})
            return {"errcode": 0, "errmsg": ""}
        except Exception as e:
            return {"errcode": -1, "errmsg": str(e)}

    # ── 公开接口 ─────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    def get_reply_req_id(self, msg_id: str) -> str | None:
        with self._reply_lock:
            return self._reply_req_ids.get(str(msg_id or "").strip())

    def pop_reply_req_id(self, msg_id: str) -> str | None:
        with self._reply_lock:
            return self._reply_req_ids.pop(str(msg_id or "").strip(), None)

    def start(self) -> None:
        """Sync start — spawns daemon thread with event loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown_event = threading.Event()

        def _connect():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._daemon_loop = loop
            loop.run_until_complete(self.connect())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._daemon_loop = None

        self._thread = threading.Thread(target=_connect, daemon=True)
        self._thread.start()

    async def close(self) -> None:
        """Close connection and stop daemon thread."""
        self._running = False
        self._connected = False
        self._authenticated = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

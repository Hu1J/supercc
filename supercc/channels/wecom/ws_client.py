"""企业微信 WebSocket 客户端（aiohttp 直连实现，替代 wecom-aibot-sdk-python）。"""
from __future__ import annotations

import aiohttp
import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 20.0

APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_PING = "ping"

CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
NON_RESPONSE_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK, APP_CMD_EVENT_CALLBACK}


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 客户端（aiohttp 直连实现）。

    替代 wecom-aibot-sdk-python，直接与 WeCom WS 网关交互，
    手动实现协议命令（aibot_subscribe, aibot_msg_callback, aibot_send_msg,
    aibot_respond_msg, ping）。
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
        self._on_message = on_message
        self._ws_url = ws_url
        self._session: Any = None
        self._ws: Any = None
        self._running = False
        self._reply_req_ids: dict[str, str] = {}  # msgid -> req_id (for reply correlation)
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._device_id = uuid.uuid4().hex
        self._thread: Optional[threading.Thread] = None
        self._daemon_loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[threading.Event] = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: dict) -> str:
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_json(raw: Any) -> Optional[dict]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Failed to parse WeCom payload: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    async def _open_connection(self) -> None:
        """Open and authenticate websocket connection."""
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        # Subscribe
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
        await self._wait_for_handshake(req_id)

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
                payload = self._parse_json(msg.data)
                if not payload:
                    continue
                if payload.get("cmd") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    errcode = payload.get("errcode", 0)
                    if errcode not in {0, None}:
                        raise RuntimeError(f"{payload.get('errmsg', 'auth failed')} (errcode={errcode})")
                    return payload
                logger.debug("[WeComWS] Ignoring pre-auth payload: %s", payload.get("cmd"))
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WeCom websocket closed during authentication")

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
            raise RuntimeError("WeCom websocket is not connected")
        await self._ws.send_json(payload)

    async def _send_request(
        self, cmd: str, body: dict, timeout: float = 15.0
    ) -> dict:
        """Send request and await correlated response."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        req_id = self._new_req_id(cmd)
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(req_id, None)

    async def connect(self) -> None:
        """Connect to WeCom WS gateway (async, called from main loop)."""
        await self._open_connection()
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("[WeComWS] Connected to WeCom WS gateway")

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors."""
        reconnect_backoff = [2, 5, 10, 30, 60]
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
                logger.warning("[WeComWS] WebSocket error: %s", exc)
                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))

                delay = reconnect_backoff[min(backoff_idx, len(reconnect_backoff) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)

                try:
                    await self._open_connection()
                    backoff_idx = 0
                    logger.info("[WeComWS] Reconnected")
                except Exception as reconnect_exc:
                    logger.warning("[WeComWS] Reconnect failed: %s", reconnect_exc)

    async def _read_events(self) -> None:
        """Read websocket frames until connection closes."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WeCom websocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send application-level pings."""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._send_json({
                        "cmd": APP_CMD_PING,
                        "headers": {"req_id": self._new_req_id("ping")},
                        "body": {},
                    })
                except Exception as exc:
                    logger.debug("[WeComWS] Heartbeat failed: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _dispatch_payload(self, payload: dict) -> None:
        """Route inbound websocket payloads."""
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")

        # Correlated response
        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return

        # Inbound message callback
        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return

        # Ignore pings and event callbacks
        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
            return

        logger.debug("[WeComWS] Ignoring websocket payload: %s", cmd or payload)

    def _fail_pending_responses(self, exc: Exception) -> None:
        """Fail all outstanding request futures."""
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending_responses.pop(req_id, None)

    async def _on_message(self, payload: dict) -> None:
        """Process inbound message callback."""
        body = payload.get("body")
        if not isinstance(body, dict):
            return

        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
        req_id = self._payload_req_id(payload)

        # Store reply_req_id for this message
        if msg_id and req_id:
            self._reply_req_ids[msg_id] = req_id

        if self._on_message:
            await self._on_message(body)

    def get_reply_req_id(self, msg_id: str) -> str | None:
        """Get stored reply_req_id for a message (for reply correlation)."""
        return self._reply_req_ids.get(str(msg_id or "").strip())

    def pop_reply_req_id(self, msg_id: str) -> str | None:
        """Pop and return reply_req_id (for one-time reply use)."""
        return self._reply_req_ids.pop(str(msg_id or "").strip(), None)

    def start(self) -> None:
        """Start connection synchronously (non-blocking, schedules tasks in thread)."""
        if self._thread is not None and self._thread.is_alive():
            return  # Already running, ignore subsequent calls
        self._shutdown_event = threading.Event()
        def _connect():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._daemon_loop = loop
            loop.run_until_complete(self.connect())
            # Block until shutdown is set, then exit cleanly
            self._shutdown_event.wait()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._daemon_loop = None
        self._thread = threading.Thread(target=_connect, daemon=True)
        self._thread.start()

    async def close(self) -> None:
        """Disconnect from WeCom and stop the daemon thread cleanly."""
        self._running = False
        # Signal the daemon thread to stop its event loop
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        # Close the websocket (from daemon's loop via run_coroutine_threadsafe, non-blocking)
        if self._ws and not self._ws.closed and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._daemon_loop)
        # Wait for daemon thread to exit (it will after ws closes and connect() returns)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._fail_pending_responses(RuntimeError("WeCom client closed"))
        await self._cleanup_ws()

    # ── Outbound API (used by WeComClient) ────────────────────────────────

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        """Send markdown message via aibot_send_msg."""
        return await self._send_request(
            APP_CMD_SEND,
            {
                "chatid": chat_id,
                "msgtype": "markdown",
                "markdown": {"content": content[:4000]},
            },
        )

    async def send_text(self, chat_id: str, content: str) -> dict:
        """Send plain text message via aibot_send_msg."""
        return await self._send_request(
            APP_CMD_SEND,
            {
                "chatid": chat_id,
                "msgtype": "text",
                "text": {"content": content[:4000]},
            },
        )

    async def reply_stream(
        self,
        reply_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
    ) -> dict:
        """Send streaming reply via aibot_respond_msg with stream_id."""
        if not reply_req_id:
            raise ValueError("reply_req_id is required for reply_stream")
        return await self._send_request(
            APP_CMD_RESPONSE,
            {
                "msgtype": "markdown",
                "markdown": {"content": content[:4000]},
                "stream_id": stream_id,
                "finish": finish,
            },
            timeout=30.0,
        )

    async def reply_text(
        self,
        reply_req_id: str,
        content: str,
    ) -> dict:
        """Send plain text reply via aibot_respond_msg."""
        if not reply_req_id:
            raise ValueError("reply_req_id is required for reply_text")
        return await self._send_request(
            APP_CMD_RESPONSE,
            {
                "msgtype": "text",
                "text": {"content": content[:4000]},
            },
            timeout=30.0,
        )

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
        """Send a message via aibot_send_msg with flexible message types."""
        body: dict = {"chatid": chat_id, "msgtype": msgtype}
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
        return await self._send_request(APP_CMD_SEND, body)
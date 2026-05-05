"""WeCom WebSocket long-connection client."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class WeComIncomingMessage:
    """Parsed incoming message from WeCom."""
    message_id: str
    chat_id: str
    user_open_id: str
    content: str
    message_type: str
    create_time: str = ""
    parent_id: str = ""
    thread_id: str = ""
    raw_content: str = ""
    is_group_chat: bool = False
    chat_type: str = "single"
    mention_bot: bool = False
    mention_ids: list[str] = field(default_factory=list)
    group_name: str = ""


MessageCallback = Callable[[WeComIncomingMessage], Awaitable[None]]


class WeComWSClient:
    """Manages WebSocket connection to WeCom AIBot gateway."""

    WS_URL = "wss://openws.work.weixin.qq.com"
    RECONNECT_DELAY = 5

    def __init__(
        self,
        bot_id: str,
        bot_secret: str,
        bot_name: str = "Claude",
        on_message: MessageCallback | None = None,
    ):
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self.bot_name = bot_name
        self._on_message = on_message
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False

    def _build_ws_url(self) -> str:
        import urllib.parse
        params = {
            "botId": self.bot_id,
            "secret": self.bot_secret,
        }
        return f"{self.WS_URL}?{urllib.parse.urlencode(params)}"

    def _parse_message(self, payload: dict) -> WeComIncomingMessage | None:
        try:
            msgid = payload.get("msgid") or payload.get("msgId") or ""
            chattype = payload.get("chattype") or payload.get("chatType") or "single"
            chatid = payload.get("chatid") or payload.get("chatId") or ""
            from_data = payload.get("from") or {}
            userid = from_data.get("userid") or from_data.get("userId") or "" if isinstance(from_data, dict) else ""
            msgtype = payload.get("msgtype") or payload.get("msgType") or "text"

            content = ""
            if msgtype == "text":
                text_data = payload.get("text") or {}
                content = text_data.get("content") or "" if isinstance(text_data, dict) else ""
            elif msgtype == "markdown":
                md_data = payload.get("markdown") or {}
                content = md_data.get("content") or "" if isinstance(md_data, dict) else ""
            elif msgtype == "image":
                content = "[image]"
            elif msgtype == "file":
                content = "[file]"

            mentions = payload.get("mention") or payload.get("mentions") or []
            mention_ids: list[str] = []
            mention_bot = False
            if mentions and isinstance(mentions, list):
                for m in mentions:
                    if isinstance(m, dict):
                        uid = m.get("userid") or m.get("userId") or ""
                        if uid:
                            mention_ids.append(uid)
                        if uid == self.bot_id:
                            mention_bot = True

            return WeComIncomingMessage(
                message_id=msgid,
                chat_id=chatid,
                user_open_id=userid,
                content=content,
                message_type=msgtype,
                create_time=str(payload.get("createTime") or payload.get("create_time") or ""),
                raw_content=json.dumps(payload, ensure_ascii=False),
                is_group_chat=chattype == "group",
                chat_type=chattype,
                mention_bot=mention_bot,
                mention_ids=mention_ids,
            )
        except Exception as e:
            logger.warning(f"Failed to parse WeCom message: {e}")
            return None

    async def _ws_loop(self) -> None:
        """Main WebSocket connection loop with auto-reconnect."""
        url = self._build_ws_url()
        while self._running:
            try:
                logger.info(f"Connecting to WeCom WebSocket: {self.WS_URL}")
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()

                async with self._session.ws_connect(url, heartbeat=30.0) as ws:
                    self._ws = ws
                    logger.info("WeCom WebSocket connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                                logger.debug(f"WeCom raw message: {msg.data[:500]}")
                                parsed = self._parse_message(payload)
                                if parsed and self._on_message:
                                    try:
                                        await self._on_message(parsed)
                                    except Exception:
                                        logger.exception("Error in WeCom message callback")
                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON from WeCom: {msg.data[:200]}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WeCom WebSocket error: {ws.exception()}")
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                            logger.info("WeCom WebSocket closed")
                            break
            except asyncio.CancelledError:
                logger.info("WeCom WebSocket loop cancelled")
                break
            except Exception as e:
                logger.warning(f"WeCom WebSocket error: {e}")

            if self._running:
                logger.info(f"Reconnecting in {self.RECONNECT_DELAY}s...")
                await asyncio.sleep(self.RECONNECT_DELAY)

    def start(self) -> None:
        """Start the WebSocket connection (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def send_message(self, chat_id: str, payload: dict) -> None:
        """Send a message via WebSocket.

        Payload shape: {"msgtype": "markdown", "markdown": {"content": "..."}}
        """
        if not self._ws or self._ws.closed:
            logger.warning("WeCom WebSocket not connected, cannot send message")
            return
        message = {"chatid": chat_id, **payload}
        try:
            await self._ws.send_str(json.dumps(message, ensure_ascii=False))
            logger.debug(f"Sent message to {chat_id}: {payload.get('msgtype')}")
        except Exception as e:
            logger.warning(f"Failed to send WeCom message: {e}")

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

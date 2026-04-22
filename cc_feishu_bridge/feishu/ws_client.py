"""Feishu WebSocket long-connection client using lark-oapi ws.Client."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable, Awaitable
from unittest.mock import MagicMock

import lark_oapi as lark

from cc_feishu_bridge.feishu.client import IncomingMessage

logger = logging.getLogger(__name__)


def _detect_media_type_from_content(parsed: dict) -> str | None:
    """Detect media type from parsed JSON content.

    Handles four Feishu content formats:
      1. Simple media: {"image_key": "..."} or {"file_key": "...", "duration": ...}
      2. Rich post (media+text): {"content": [[{"tag": "img/file/audio", ...}], [{"tag": "text", ...}]]}
      3. Standalone rich post file: {"content": [[{"tag": "file", "file_key": "..."}]]}
      4. Simple text: {"text": "..."}
    """
    # Format 1: simple {"image_key": ...} or {"file_key": ...}
    if "image_key" in parsed:
        return "image"
    if "file_key" in parsed:
        if "duration" in parsed:
            return "audio"
        return "file"

    # Format 2 & 3: rich post with [[{tag: "img", ...}], [{tag: "text", ...}]]
    content = parsed.get("content", [])
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            tag = item.get("tag", "")
            if tag == "img" and "image_key" in item:
                return "image"
            if tag == "audio" and "file_key" in item:
                return "audio"
            if tag == "file" and "file_key" in item:
                return "file"

    return None


def _extract_text_from_content(parsed: dict) -> str:
    """Extract user text from parsed JSON content.

    Handles three Feishu content formats:
      1. Simple text: {"text": "..."}
      2. Rich post: {"content": [[{"tag": "img", ...}], [{"tag": "text", "text": "..."}]]}
      3. Empty / media-only
    """
    # Format 1: simple {"text": "..."}
    if "text" in parsed:
        return parsed.get("text", "")

    # Format 2: rich post with tag="text" nodes
    content = parsed.get("content", [])
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            if item.get("tag") == "text":
                text = item.get("text", "")
                if text:
                    parts.append(text)

    return " ".join(parts)

MessageCallback = Callable[[IncomingMessage], Awaitable[None]]


class FeishuWSClient:
    """Manages WebSocket connection to Feishu via lark-oapi ws.Client."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        bot_name: str = "Claude",
        bot_open_id: str = "",
        domain: str = "feishu",
        on_message: MessageCallback | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_name = bot_name
        self._configured_bot_open_id = bot_open_id
        self.domain = domain
        self._on_message = on_message
        self._ws_client = None
        self._handler = None
        self._probed_bot_open_id: str | None = None

    @property
    def bot_open_id(self) -> str:
        """Returns configured bot_open_id, falling back to auto-probed value."""
        return self._configured_bot_open_id or self._probed_bot_open_id or ""

    def probe_bot_info(self) -> str | None:
        """Probe bot identity via the /open-apis/bot/v1/openclaw_bot/ping API.

        Auto-discovers the bot's own open_id without requiring manual config.
        Updates _probed_bot_open_id on success.

        Returns the bot's open_id on success, None on failure.
        """
        from lark_oapi.core.model.base_request import BaseRequest
        from lark_oapi.core import HttpMethod, AccessTokenType

        client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.POST)
            .uri("/open-apis/bot/v1/openclaw_bot/ping")
            .token_types({AccessTokenType.APP})
            .body({"needBotInfo": True})
            .build()
        )
        try:
            resp = client.request(request)
            if resp.code == 0 and resp.raw and resp.raw.content:
                import json
                raw_body = json.loads(resp.raw.content)
                data = raw_body.get("data") or {}
                bot_info = data.get("pingBotInfo") or data.get("ping_bot_info") or {}
                bot_id = bot_info.get("botID") or bot_info.get("bot_id")
                if bot_id:
                    self._probed_bot_open_id = bot_id
                    logger.info(f"Auto-probed bot_open_id: {self._probed_bot_open_id}")
                    return self._probed_bot_open_id
            logger.warning(f"bot probe API failed: code={resp.code} msg={getattr(resp, 'msg', '')}")
        except Exception as e:
            logger.warning(f"bot probe API error: {e}")
        return None

    def _build_event_handler(self):
        """Build EventDispatcherHandler with p2p message callback registered."""
        builder = lark.EventDispatcherHandler.builder(
            encrypt_key="",
            verification_token="",
        )

        def wrapped_handler(event):
            """Handle incoming p2p message event."""
            if self._on_message is None:
                return
            try:
                event_data = event.event
                message = event_data.message
                sender = event_data.sender
                msg_type = getattr(message, "msg_type", "text")
                content_str = getattr(message, "content", "{}")

                # Parse JSON content for text messages
                content = content_str
                if msg_type == "text":
                    try:
                        parsed = json.loads(content_str)
                        # Determine effective message type from rich post or simple media JSON
                        effective_type = _detect_media_type_from_content(parsed)
                        if effective_type:
                            msg_type = effective_type
                        # Extract text content
                        content = _extract_text_from_content(parsed)
                    except Exception:
                        pass

                logger.debug(
                    f"Raw message — type={msg_type!r}, message_id={getattr(message, 'message_id', '')!r}, "
                    f"parent_id={getattr(message, 'parent_id', '')!r}, root_id={getattr(message, 'root_id', '')!r}, "
                    f"content={content_str!r}"
                )

                sender_id = getattr(sender, "sender_id", None)
                user_open_id = ""
                if sender_id is not None:
                    user_open_id = getattr(sender_id, "open_id", "")

                # Extract chat_type: 'p2p' or 'group' (from official plugin)
                chat_type = str(getattr(message, "chat_type", "p2p") or "p2p")
                is_group_chat = chat_type == "group"

                # Extract mentions[] to determine if bot was @mentioned.
                # Each mention: { key: "@_user_1", id: { open_id: "ou_xxx", ... }, name: "Alice" }
                # Fallback: also check raw_content for @_user_N patterns since the mentions[]
                # array may not be reliably populated via WebSocket (OpenClaw strategy).
                mention_ids: list[str] = []
                mention_bot = False
                has_text_mention = bool(re.search(r"@_user_\d+", content_str))

                if not self.bot_open_id:
                    # bot_open_id not configured — group @mention detection is unavailable.
                    # Log a warning once per process lifetime to alert the operator.
                    # Once is enough: either it's configured or it isn't.
                    if is_group_chat and not hasattr(self, "_bot_open_id_warned"):
                        logger.warning(
                            "bot_open_id is not set in config.yaml — group @mention detection "
                            "will not work. Set feishu.bot_open_id to enable group chat @CC."
                        )
                        self._bot_open_id_warned = True
                else:
                    mentions = getattr(message, "mentions", None)
                    # Log raw mentions for debugging
                    logger.debug(
                        f"[MENTION_DEBUG] is_group={is_group_chat}, mentions type={type(mentions)}, "
                        f"value={repr(mentions)[:300]}, bot_open_id={self.bot_open_id!r}"
                    )
                    if mentions:
                        for m in mentions:
                            logger.debug(f"[MENTION_DEBUG] mention item: type={type(m)}, repr={repr(m)[:300]}")
                            mid = getattr(m, "id", None)
                            logger.debug(f"[MENTION_DEBUG] mid type={type(mid)}, repr={repr(mid)[:200]}")
                            if mid is not None:
                                open_id = getattr(mid, "open_id", "") or ""
                                logger.debug(f"[MENTION_DEBUG] open_id={open_id!r}, bot={self.bot_open_id!r}, match={open_id == self.bot_open_id}")
                                if open_id:
                                    mention_ids.append(open_id)
                                    if open_id == self.bot_open_id:
                                        mention_bot = True
                    elif is_group_chat and has_text_mention:
                        mention_bot = True
                        logger.debug("Group @mention via content fallback")

                incoming = IncomingMessage(
                    message_id=getattr(message, "message_id", ""),
                    chat_id=getattr(message, "chat_id", ""),
                    user_open_id=user_open_id,
                    content=content,
                    message_type=msg_type,
                    create_time=getattr(message, "create_time", ""),
                    parent_id=getattr(message, "parent_id", ""),
                    thread_id=getattr(message, "thread_id", ""),
                    raw_content=content_str,
                    is_group_chat=is_group_chat,
                    chat_type=chat_type,
                    mention_bot=mention_bot,
                    mention_ids=mention_ids,
                    group_name=str(getattr(message, "chat_name", "") or ""),
                )
                logger.info(f"Received message from {user_open_id}: type={msg_type!r} parent_id={getattr(message, 'parent_id', '')!r} raw_content={content_str!r}")
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # No running loop (e.g., in tests) — run in a new loop
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(self._on_message(incoming))
                    loop.close()
                    return
                asyncio.ensure_future(self._on_message(incoming))
            except Exception as e:
                logger.exception(f"Error handling Feishu message: {e}")

        builder.register_p2_im_message_receive_v1(wrapped_handler)

        # Register no-op handlers for reaction events (bot adding/removing emoji reactions).
        # These events come back from Feishu but we don't need to act on them.
        def noop_handler(event):
            pass

        builder.register_p2_im_message_reaction_created_v1(noop_handler)
        builder.register_p2_im_message_reaction_deleted_v1(noop_handler)

        self._handler = builder.build()
        return self._handler

    def start(self) -> None:
        """Start the WebSocket long connection (blocking)."""
        if self._ws_client is not None:
            return

        # Auto-probe bot identity so mention detection works without manual config.
        if not self._configured_bot_open_id:
            self.probe_bot_info()

        self._handler = self._build_event_handler()
        base_url = "https://open.feishu.cn" if self.domain == "feishu" else "https://open.larksuite.com"

        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self._handler,
            domain=base_url,
            auto_reconnect=True,
        )
        logger.info(f"Starting Feishu WebSocket connection to {base_url}...")
        self._ws_client.start()

    # Expose handler for testing
    def _handle_p2p_message(self, event):
        """Internal handler for testing — calls the wrapped handler directly."""
        handler = self._build_event_handler()
        handler._processorMap.get("p2.im.message.receive_v1").f(event)

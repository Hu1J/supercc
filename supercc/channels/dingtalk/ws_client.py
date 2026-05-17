"""钉钉 WebSocket 客户端（dingtalk-stream SDK 实现）。"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Awaitable

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotHandler, ChatbotMessage, CallbackMessage
    DINGTALK_STREAM_AVAILABLE = True
except ImportError:
    DINGTALK_STREAM_AVAILABLE = False
    ChatbotHandler = object
    ChatbotMessage = Any
    CallbackMessage = Any

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]

# 重连 backoff 列表
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]


class DingTalkWSClient(ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    """
    钉钉 Chatbot WebSocket 客户端（dingtalk-stream SDK 实现）。

    继承 ChatbotHandler，处理入站消息并转发给回调。
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        on_message: MessageCallback | None = None,
    ):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_message = on_message
        self._client: Any = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: threading.Event | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._running

    def _parse_message(self, data: dict) -> dict:
        """解析 dingtalk-stream 消息为统一格式。"""
        # 获取会话 webhook（用于出站发送）
        session_webhook = data.get("sessionWebhook", "") or data.get("session_webhook", "")

        # 解析会话 ID
        conversation_id = data.get("conversationId", "") or data.get("conversation_id", "")

        # 解析发送者
        sender_id = data.get("senderId", "") or data.get("sender_id", "")
        sender_nick = data.get("senderNick", "") or data.get("sender_nick", "") or sender_id
        sender_staff_id = data.get("senderStaffId", "") or data.get("sender_staff_id", "")

        # 解析会话类型：1=单聊，2=群聊
        conversation_type = data.get("conversationType", "")

        # 解析消息内容
        text = ""
        raw_text = data.get("text", "") or data.get("content", "")
        if isinstance(raw_text, dict):
            text = raw_text.get("content", "").strip()
        elif isinstance(raw_text, str):
            text = raw_text.strip()

        # 解析消息 ID
        msg_id = data.get("msgId", "") or data.get("messageId", "") or ""

        # 解析时间戳
        create_at = data.get("createAt", "") or data.get("create_at", "")

        # 解析机器人码
        robot_code = data.get("robotCode", "") or data.get("robot_code", "") or self._app_key

        return {
            "msgid": msg_id,
            "chat_id": conversation_id or sender_id,
            "user_id": sender_id,
            "user_nick": sender_nick,
            "user_staff_id": sender_staff_id,
            "content": text,
            "conversation_type": conversation_type,
            "is_group": str(conversation_type) == "2",
            "session_webhook": session_webhook,
            "create_at": create_at,
            "robot_code": robot_code,
        }

    async def process(self, message: CallbackMessage) -> tuple[int, str]:
        """处理收到的钉钉消息（dingtalk-stream SDK 回调）。

        dingtalk-stream >= 0.20 传递 CallbackMessage.data 作为原始数据。
        """
        try:
            data = message.data
            if isinstance(data, str):
                import json as _json
                data = _json.loads(data)

            parsed = self._parse_message(data)
            logger.info(
                "[DingTalkWS] msg_id=%s, chat_id=%s, content=%s",
                parsed["msgid"][:20] if parsed["msgid"] else "None",
                parsed["chat_id"][:20] if parsed["chat_id"] else "None",
                parsed["content"][:50] if parsed["content"] else "None",
            )

            if self._on_message:
                await self._on_message(parsed)

            # 返回 ACK
            if DINGTALK_STREAM_AVAILABLE:
                from dingtalk_stream.frames import AckMessage
                return AckMessage.STATUS_OK, "OK"
            return 200, "OK"
        except Exception as exc:
            logger.error("[DingTalkWS] process error: %s", exc)
            if DINGTALK_STREAM_AVAILABLE:
                from dingtalk_stream.frames import AckMessage
                return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
            return 500, "error"

    async def _run_stream(self) -> None:
        """运行流客户端，自动重连。"""
        import dingtalk_stream

        backoff_idx = 0
        while self._running:
            try:
                credential = dingtalk_stream.Credential(
                    self._app_key, self._app_secret
                )
                self._client = dingtalk_stream.DingTalkStreamClient(credential)
                self._client.register_callback_handler(
                    dingtalk_stream.ChatbotMessage.TOPIC, self
                )

                logger.info("[DingTalkWS] Connecting to DingTalk stream...")
                await self._client.start()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[DingTalkWS] Stream error: %s", exc)

                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                logger.info("[DingTalkWS] Reconnecting in %ds...", delay)
                await asyncio.sleep(delay)

    def start(self) -> None:
        """启动 WebSocket 客户端（同步，非阻塞）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._shutdown_event = threading.Event()

        def _connect():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._daemon_loop = loop
            loop.run_until_complete(self._run_stream())
            # Block until shutdown
            self._shutdown_event.wait()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._daemon_loop = None

        self._thread = threading.Thread(target=_connect, daemon=True)
        self._thread.start()

    async def close(self) -> None:
        """关闭 WebSocket 连接。"""
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._client and hasattr(self._client, "close"):
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("[DingTalkWS] Disconnected")

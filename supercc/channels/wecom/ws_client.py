"""企业微信 WebSocket 客户端 - 官方 wecom-aibot-sdk 封装。

替换自写 WS 协议实现，使用官方 SDK 保持协议兼容和自动重连。
对外接口保持不变：start/close/is_connected/send_markdown/send_text/reply_stream/reply_text/pop_reply_req_id。
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any, Callable, Awaitable, Optional

from wecom_aibot_sdk import WSClient, WSClientOptions, WsFrame

logger: Any = __import__("logging").getLogger(__name__)

MessageCallback = Callable[[dict], Awaitable[None]]

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"


class WeComWSClient:
    """
    企业微信 AI Bot WebSocket 客户端（官方 SDK 封装）。

    对外接口与自写实现保持一致，内部委托给 wecom_aibot_sdk.WSClient。
    额外维护 msgid→req_id 映射表（用于 reply 相关接口）。
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

        # SDK 选项
        self._options = WSClientOptions(
            bot_id=bot_id,
            secret=bot_secret,
            ws_url=ws_url,
            reconnect_interval=1000,
            max_reconnect_attempts=10,
            heartbeat_interval=30000,
        )

        # SDK 客户端
        self._client = WSClient(self._options)

        # msgid → req_id 映射（用于 reply 接口）
        self._reply_req_ids: dict[str, str] = {}
        self._reply_lock = threading.Lock()

        # Daemon 线程状态
        self._thread: Optional[threading.Thread] = None
        self._daemon_loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[threading.Event] = None
        self._started = False

        # 注册默认消息处理器（触发 _on_message 回调）
        self._client.on("message", self._sdk_message_handler)

    # ── 内部消息处理 ─────────────────────────────────────────────────────

    async def _sdk_message_handler(self, frame: WsFrame) -> None:
        """SDK 消息回调：提取 msgid/req_id 映射，转发给业务回调。"""
        body = frame.body
        if not isinstance(body, dict):
            return

        msg_id = str(body.get("msgid") or "")
        req_id = str((frame.headers or {}).get("req_id") or "")

        if msg_id and req_id:
            with self._reply_lock:
                self._reply_req_ids[msg_id] = req_id
            logger.info(f"[WeComWS] stored _reply_req_ids[{msg_id[:20]}] = {req_id[:20] if req_id else 'None'}")

        if self._on_message:
            await self._on_message(body)

    # ── 公开接口（与自写实现保持一致）──────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    def get_reply_req_id(self, msg_id: str) -> str | None:
        """获取存储的回复请求 ID。"""
        with self._reply_lock:
            return self._reply_req_ids.get(str(msg_id or "").strip())

    def pop_reply_req_id(self, msg_id: str) -> str | None:
        """弹出并返回回复请求 ID（一次性使用）。"""
        with self._reply_lock:
            return self._reply_req_ids.pop(str(msg_id or "").strip(), None)

    async def reply_stream(
        self,
        reply_req_id: str,
        stream_id: str,
        content: str,
        finish: bool = False,
    ) -> dict:
        """发送流式回复。"""
        if not reply_req_id:
            raise ValueError("reply_req_id is required for reply_stream")

        headers = {"req_id": reply_req_id}
        from wecom_aibot_sdk import generate_req_id
        frame = await self._client.reply_stream(
            frame=headers,
            stream_id=stream_id,
            content=content[:4000],
            finish=finish,
        )
        return {"errcode": frame.errcode, "errmsg": frame.errmsg or ""}

    async def reply_text(
        self,
        reply_req_id: str,
        content: str,
    ) -> dict:
        """发送纯文本回复。"""
        if not reply_req_id:
            raise ValueError("reply_req_id is required for reply_text")

        headers = {"req_id": reply_req_id}
        frame = await self._client.reply(
            frame=headers,
            body={"msgtype": "text", "text": {"content": content[:4000]}},
            cmd="aibot_respond_msg",
        )
        return {"errcode": frame.errcode, "errmsg": frame.errmsg or ""}

    async def send_markdown(self, chat_id: str, content: str) -> dict:
        """主动发送 markdown 消息。"""
        frame = await self._client.send_message(
            chatid=chat_id,
            body={"msgtype": "markdown", "markdown": {"content": content[:4000]}},
        )
        return {"errcode": frame.errcode, "errmsg": frame.errmsg or ""}

    async def send_text(self, chat_id: str, content: str) -> dict:
        """主动发送文本消息。"""
        frame = await self._client.send_message(
            chatid=chat_id,
            body={"msgtype": "text", "text": {"content": content[:4000]}},
        )
        return {"errcode": frame.errcode, "errmsg": frame.errmsg or ""}

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
        """通用消息发送接口。"""
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

        frame = await self._client.send_message(chatid=chat_id, body=body)
        return {"errcode": frame.errcode, "errmsg": frame.errmsg or ""}

    # ── 生命周期管理 ─────────────────────────────────────────────────────

    def start(self) -> None:
        """同步启动：在独立线程中运行 asyncio event loop。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown_event = threading.Event()

        def _connect():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._daemon_loop = loop
            loop.run_until_complete(self._client.connect_async())
            self._shutdown_event.wait()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._daemon_loop = None

        self._thread = threading.Thread(target=_connect, daemon=True)
        self._thread.start()
        self._started = True

    async def close(self) -> None:
        """关闭连接，停止 daemon 线程。"""
        self._started = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        if self._client.is_connected and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._daemon_loop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

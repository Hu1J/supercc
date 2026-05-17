"""WhatsApp 插件的 Thin Client：连接核心 WebSocket 服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from supercc.core.protocol import JsonRpcRequest
from supercc.channels.whatsapp.bridge_client import WhatsAppBridgeClient
from supercc.channels.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)


class WhatsAppCoreWSClient:
    """
    WhatsApp Thin Client：桥接 Node.js bridge 与 SuperCC Core WS。

    接收流程：Node.js bridge（长轮询）→ bridge_client → core_client → Core WS
    发送流程：Core WS → core_client → WhatsAppClient → bridge_client → Node.js bridge
    """

    def __init__(
        self,
        core_url: str,
        bridge_port: int,
        allowed_users: list[str],
        project_path: str = "",
    ):
        """
        Args:
            core_url: SuperCC Core WebSocket 地址，如 ws://127.0.0.1:8080
            bridge_port: Node.js bridge HTTP 端口
            allowed_users: 允许的用户 ID 列表
            project_path: 项目路径（用于记忆等功能）
        """
        self._core_url = core_url
        self._bridge_port = bridge_port
        self._allowed_users = set(allowed_users)
        self._project_path = project_path

        self._bridge = WhatsAppBridgeClient(port=bridge_port)
        self._client = WhatsAppClient(bridge_port=bridge_port)

        self._ws: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = False
        self._pending: dict[str, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

        # 消息序列号
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def connect(self) -> None:
        """连接到 Core WebSocket 服务。"""
        import asyncio

        logger.info("Connecting to Core at %s", self._core_url)
        host = self._core_url.replace("ws://", "").split(":")[0]
        port = int(self._core_url.replace("ws://", "").split(":")[1])

        self._ws, self._writer = await asyncio.open_connection(host, port)
        self._running = True

        # 启动接收循环
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("Connected to Core")

    async def _recv_loop(self) -> None:
        """接收 Core WS 消息。"""
        assert self._ws is not None
        buffer = ""

        while self._running:
            try:
                data = await self._ws.read(4096)
                if not data:
                    logger.warning("Core WS closed")
                    break

                buffer += data.decode("utf-8")
                # 按行分割（\n 分隔的 JSON-RPC 消息）
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    await self._handle_core_message(line)
            except Exception as e:
                logger.error("Error in recv loop: %s", e)
                break

        self._running = False

    async def _handle_core_message(self, raw: str) -> None:
        """处理 Core 发来的消息。"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from core: %s", raw[:100])
            return

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "response" or method == "error":
            # JSON-RPC Response
            if msg_id and str(msg_id) in self._pending:
                fut = self._pending.pop(str(msg_id))
                fut.set_result(msg)
            return

        if method == "bing_event":
            # 流式事件
            event_type = params.get("event", "")
            if event_type == "tool_call":
                await self._do_tool_call(params)
            elif event_type == "text":
                await self._do_send_text(params)
            elif event_type == "done":
                # 消息发送完成
                pass
            return

        # 其他方法暂不处理
        logger.debug("Unhandled core method: %s", method)

    async def _do_tool_call(self, params: dict) -> None:
        """处理工具调用事件。"""
        tool_name = params.get("tool", "")
        tool_input = params.get("input", {})
        chat_id = params.get("chat_id", "")

        if tool_name == "send_text":
            text = tool_input.get("text", "")
            reply_to = tool_input.get("reply_to")
            await self._client.send_text(chat_id, text, reply_to)

    async def _do_send_text(self, params: dict) -> None:
        """处理文本发送事件。"""
        chat_id = params.get("chat_id", "")
        text = params.get("text", "")
        reply_to = params.get("reply_to")

        if text:
            await self._client.send_text(chat_id, text, reply_to)

    async def send_message(self, body: dict[str, Any]) -> None:
        """
        发送消息到 Core（JSON-RPC Request）。

        Args:
            body: 包含 msgid, chat_id, user_id, content 等字段的字典
        """
        msgid = body.get("msgid", "")
        chat_id = body.get("chat_id", "")
        sender_id = body.get("sender_id", "")
        content = body.get("content", "")

        # 白名单检查
        if self._allowed_users and sender_id not in self._allowed_users:
            logger.debug("User %s not in allowed list, ignoring", sender_id)
            return

        # 构建入站消息
        inbound = {
            "msgid": msgid,
            "chat_id": chat_id,
            "user_id": sender_id,
            "content": content,
        }

        # 发送 JSON-RPC Request
        req = JsonRpcRequest(
            method="user_message",
            params={
                "platform": "whatsapp",
                "chat_id": chat_id,
                "user_id": sender_id,
                "content": content,
                "message_id": msgid,
            },
        )
        await self._send_rpc(req)

    async def _send_rpc(self, req: JsonRpcRequest) -> None:
        """发送 JSON-RPC 请求。"""
        if not self._writer:
            return
        raw = req.model_dump_json() + "\n"
        self._writer.write(raw.encode("utf-8"))
        await self._writer.drain()

    async def close(self) -> None:
        """关闭连接。"""
        logger.info("Closing WhatsApp core client...")
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        logger.info("WhatsApp core client closed")


async def run_bridge_polling(
    bridge: WhatsAppBridgeClient,
    on_message: Callable[[dict[str, Any]], None],
    poll_interval: float = 1.0,
) -> None:
    """
    长轮询 Node.js bridge 获取新消息。

    Args:
        bridge: WhatsAppBridgeClient 实例
        on_message: 收到消息时的回调
        poll_interval: 轮询间隔（秒）
    """
    while True:
        try:
            messages = await bridge.get_messages()
            for msg in messages:
                await on_message(msg)
        except Exception as e:
            logger.warning("Bridge polling error: %s", e)

        await asyncio.sleep(poll_interval)

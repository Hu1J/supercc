"""核心消息执行器：处理 InboundMessage，调用 Claude，结果发回插件。

这是核心真正执行 AI 推理的地方。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from core.protocol import (
    InboundMessage, OutboundMessage,
    MessageType, Event,
)
from core.session import SessionManager
from core.worker import WorkerPool

logger = logging.getLogger(__name__)


class CoreExecutor:
    """
    核心消息执行器。

    接收 InboundMessage，按 SessionKey 获取/创建 Session，
    通过 WorkerPool.execute() 执行 Claude 查询，
    收集流式输出并回调。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        worker_pool: WorkerPool,
    ):
        self.sessions = session_manager
        self.pool = worker_pool

    async def execute(
        self,
        inbound: InboundMessage,
        on_stream: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> OutboundMessage:
        """
        处理一条 InboundMessage，返回 OutboundMessage。

        on_stream: 流式输出的回调（每收到一个 chunk 调用一次）
        """
        key = inbound.session_key
        user_open_id = inbound.user_open_id or ""

        # 获取或创建 Session
        session = self.sessions.get_or_create_session(key, user_open_id)

        # 构建 prompt（从 inbound.content）
        prompt = self._build_prompt(inbound)

        # 流式回调包装
        accumulated = []

        async def _stream_callback(msg: Any) -> None:
            if msg.content:
                accumulated.append(msg.content)
                if on_stream:
                    chunk = OutboundMessage(
                        event=Event.STREAM_CHUNK,
                        session_key=key,
                        message_id=inbound.message_id,
                        content=msg.content,
                        message_type=MessageType.TEXT,
                    )
                    await on_stream(chunk)
            elif msg.tool_name and on_stream:
                tool_msg = OutboundMessage(
                    event=Event.TOOL_CALL,
                    session_key=key,
                    message_id=inbound.message_id,
                    content=f"[{msg.tool_name}]",
                    message_type=MessageType.TOOL_CALL,
                    extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                )
                await on_stream(tool_msg)

        # 执行查询
        try:
            result, cost = await self.pool.execute(
                key=key,
                session_id=session.session_id,
                integration=None,  # 实际由 pool.acquire 内部创建
                prompt=prompt,
                on_stream=_stream_callback,
            )
        except Exception as e:
            logger.exception(f"[CoreExecutor] execute error for {key}")
            result = f"错误: {e}"
            cost = 0.0

        # 更新 Session 统计
        self.sessions.update_session(
            session_id=session.session_id,
            cost=cost,
            message_increment=1,
            update_last_message=True,
        )

        # 存储消息
        self.sessions.store_message(
            message_id=inbound.message_id,
            session_id=session.session_id,
            chat_id=key.chat_id,
            user_open_id=user_open_id,
            message_type=inbound.message_type.value,
            raw_content=inbound.extra.get("raw", ""),
            content=inbound.content,
            direction="incoming",
        )

        return OutboundMessage(
            event=Event.RESPONSE,
            session_key=key,
            message_id=inbound.message_id,
            content=result,
            message_type=MessageType.TEXT,
        )

    def _build_prompt(self, inbound: InboundMessage) -> str:
        """从 inbound 构建发送给 Claude 的 prompt。"""
        content = inbound.content

        # 注入平台信息
        extra = inbound.extra
        if extra.get("is_group_chat"):
            content = f"[群聊 {extra.get('group_name', '')}] {content}"

        return content

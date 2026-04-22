"""Claude Code integration via claude-agent-sdk."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ClaudeMessage:
    content: str
    is_final: bool = False
    tool_name: str | None = None
    tool_input: str | None = None


StreamCallback = Callable[[ClaudeMessage], Awaitable[None]]


class ClaudeIntegration:
    def __init__(
        self,
        cli_path: str = "claude",
        max_turns: int = 50,
        approved_directory: str | None = None,
    ):
        if cli_path == "claude":
            resolved = shutil.which("claude")
            self.cli_path = resolved if resolved else cli_path
        else:
            self.cli_path = cli_path
        self.max_turns = max_turns
        self.approved_directory = approved_directory
        self._options: Any = None  # 持久化的 ClaudeAgentOptions
        self._system_prompt_append: str | None = None
        self._query_lock = asyncio.Lock()  # 保证同一时间只有一个 query 在执行
        self.stop_event = asyncio.Event()  # /stop 信号，listener 收到后 interrupt

    def mark_system_prompt_stale(self) -> None:
        """标记 system prompt 已过期，下次 query 时重新初始化。"""
        self._options = None

    # -------------------------------------------------------------------------
    # Options 初始化
    # -------------------------------------------------------------------------

    def _init_options(self, system_prompt_append: str | None = None,
                      continue_conversation: bool = True) -> None:
        """
        构建持久化 ClaudeAgentOptions，供整个 worker 生命周期复用。
        system prompt 更新只需重新调用此方法。
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from cc_feishu_bridge.claude.memory_tools import get_memory_mcp_server
        from cc_feishu_bridge.claude.feishu_file_tools import get_feishu_file_mcp_server
        from cc_feishu_bridge.claude.cron_tools import get_cron_mcp_server

        memory_server = get_memory_mcp_server()
        feishu_server = get_feishu_file_mcp_server()
        cron_server = get_cron_mcp_server()

        options = ClaudeAgentOptions(
            cwd=self.approved_directory or ".",
            # NOTE: 不传 cli_path，让 SDK 使用内置的 bundled CLI。
            # 显式指定 cli_path 在 Windows 上会导致 initialize() 超时。
            include_partial_messages=True,
            permission_mode="bypassPermissions",
            continue_conversation=continue_conversation,
            mcp_servers={
                "memory": memory_server,
                "feishu_file": feishu_server,
                "cron": cron_server,
            },
        )

        if system_prompt_append:
            options.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt_append,
            }

        self._options = options
        self._system_prompt_append = system_prompt_append

    # -------------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------------

    async def query(
        self,
        prompt: str,
        on_stream: StreamCallback | None = None,
        on_start: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[str, str | None, float]:
        """
        每个 query 内部创建独立 client，用完即销毁。
        启动时额外创建一个 listener 协程监听 stop_event，
        收到 /stop 信号时立即 interrupt 并 await consume_task。
        on_start 回调在 _query_lock 拿到后立即调用（异步），用于显示 typing 等前置状态。
        """
        if self._options is None:
            raise RuntimeError(
                "ClaudeIntegration not initialized. Call _init_options() first."
            )

        import time as time_module

        async with self._query_lock:
            if on_start:
                await on_start()
            t_query = time_module.time()
            self.stop_event.clear()

            # 每次 query 创建新 client，用完即销毁
            from claude_agent_sdk import ClaudeSDKClient
            async with ClaudeSDKClient(options=self._options) as client:
                # 发送 prompt
                await client.query(prompt=prompt)

                # 后台消费任务
                async def _consume():
                    result_text = ""
                    result_session_id = None
                    result_cost = 0.0
                    async for message in client.receive_response():
                        msg_type = type(message).__name__
                        if msg_type == "ResultMessage":
                            result_text = getattr(message, "result", "") or ""
                            result_session_id = getattr(message, "session_id", None)
                            result_cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                            elapsed = time_module.time() - t_query
                            logger.info(
                                f"[query] <<< session_id={result_session_id!r}, "
                                f"cost={result_cost!r}, elapsed={elapsed:.1f}s"
                            )
                        if on_stream:
                            parsed = self._parse_message(message)
                            if parsed:
                                await on_stream(parsed)
                    return (result_text, result_session_id, result_cost)

                consume_task = asyncio.create_task(_consume())

                # Listener：监听 stop_event，收到信号时 interrupt
                async def _listener():
                    await self.stop_event.wait()
                    await client.interrupt()
                    await consume_task
                    logger.info("[listener] stop handling done")

                listener_task = asyncio.create_task(_listener())
                try:
                    result = await consume_task
                finally:
                    listener_task.cancel()
            return result

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _parse_message(self, message) -> ClaudeMessage | None:
        """Parse SDK Message into ClaudeMessage."""
        import json

        msg_type = type(message).__name__

        if msg_type == "AssistantMessage":
            for block in getattr(message, "content", []):
                block_type = type(block).__name__
                if block_type == "TextBlock":
                    text = getattr(block, "text", "")
                    if text:
                        return ClaudeMessage(content=text, is_final=False)
                elif block_type == "ToolUseBlock":
                    tool_name = getattr(block, "name", "Unknown")
                    tool_input = getattr(block, "input", "")
                    if isinstance(tool_input, dict):
                        tool_input = json.dumps(tool_input, ensure_ascii=False)
                    return ClaudeMessage(
                        content="",
                        is_final=False,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )

        elif msg_type == "ResultMessage":
            return None

        return None

"""Claude Code integration via claude-agent-sdk."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

from supercc.core.mcps.codex_exec import CodexRunTailingRunner, CodexStreamEvent

logger = logging.getLogger(__name__)


@dataclass
class ClaudeMessage:
    content: str
    is_final: bool = False
    tool_name: str | None = None
    tool_input: str | None = None


# Union type for stream events from both Claude and Codex
StreamItem = ClaudeMessage | CodexStreamEvent
StreamCallback = Callable[[StreamItem], Awaitable[None]]


class ClaudeIntegration:
    def __init__(
        self,
        cli_path: str = "claude",
        max_turns: int = 50,
        approved_directory: str | None = None,
        memory_only: bool = False,
    ):
        if cli_path == "claude":
            resolved = shutil.which("claude")
            self.cli_path = resolved if resolved else cli_path
        else:
            self.cli_path = cli_path
        self.max_turns = max_turns
        self.approved_directory = approved_directory
        self.memory_only = memory_only
        self._options: Any = None  # 持久化的 ClaudeAgentOptions
        # _options 对应的 model 配置，用于检测切换后是否需要重建（auth_token, base_url, model 三元组）
        self._options_model_config: tuple[str, str, str] | None = None
        self._system_prompt_append: str | None = None
        self._continue_conversation: bool = True  # 持久化
        self._new_session_requested: bool = False  # /new 一次性标志，下一次 query 消耗
        self._query_lock = asyncio.Lock()  # 保证同一时间只有一个 query 在执行
        self.stop_event = asyncio.Event()  # /stop 信号，listener 收到后 interrupt
        self._codex_capture_tasks: set[asyncio.Task] = set()

    def mark_system_prompt_stale(self) -> None:
        """标记 system prompt 已过期，下次 query 时重新初始化。"""
        self._options = None
        self._options_model_config = None

    def _ensure_claude_onboarding(self) -> None:
        """强制写入 ~/.claude.json，确保 hasCompletedOnboarding: true（幂等）"""
        import json
        from pathlib import Path
        claude_json_path = Path.home() / ".claude.json"
        data = {}
        if claude_json_path.exists():
            try:
                with open(claude_json_path) as f:
                    data = json.load(f)
            except Exception:
                pass
        if data.get("hasCompletedOnboarding"):
            return  # 已完成，无需重复写
        data["hasCompletedOnboarding"] = True
        try:
            claude_json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(claude_json_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to write %s: %s", claude_json_path, e)

    # -------------------------------------------------------------------------
    # Options 初始化
    # -------------------------------------------------------------------------

    def _init_options(self, system_prompt_append: str | None = None,
                      continue_conversation: bool | None = None,
                      channel: str = "feishu",
                      session_id: str | None = None,
                      resume: str | None = None) -> None:
        """
        构建持久化 ClaudeAgentOptions，供整个 worker 生命周期复用。
        system prompt 更新只需重新调用此方法。
        _new_session_requested 标志由 /new 设置，只对下一次 query 生效，之后自动清除。
        channel: 当前聊天频道（feishu/dingtalk/wechat 等），用于决定注册哪些 MCP 工具。
        session_id: 首次调用时传入 UUID，创建新会话
        resume: 后续调用时传入 UUID，继续该会话
        """
        # 检测 model 配置是否已切换（auth_token、base_url、model 任一变化都需重建）
        try:
            from supercc.core.models.model_config import get_model_env
            env = get_model_env()
            current_config = (
                env.ANTHROPIC_AUTH_TOKEN if env else "",
                env.ANTHROPIC_BASE_URL if env else "",
                env.ANTHROPIC_MODEL if env else "",
            )
            if current_config != self._options_model_config:
                self._options = None
        except Exception:
            pass

        from claude_agent_sdk import ClaudeAgentOptions
        from supercc.core.claude.supercc_tools import get_supercc_mcp_server, get_memory_only_mcp_server

        # 一次性标志消耗：/new 设置后，只对下一次 query 生效，之后清除
        if self._new_session_requested:
            self._new_session_requested = False
            self._continue_conversation = False
        elif continue_conversation is not None:
            self._continue_conversation = continue_conversation
        # else: 复用 self._continue_conversation（默认为 True）

        if self.memory_only:
            supercc_server = get_memory_only_mcp_server()
        else:
            include_feishu = channel == "feishu"
            include_wecom = channel == "wecom"
            supercc_server = get_supercc_mcp_server(include_feishu=include_feishu, include_wecom=include_wecom)

        mcp_servers = {
            "SuperCC": supercc_server,
        }
        try:
            from supercc.config import get_config
            from supercc.core.mcps.codex_mcp import (
                build_supercc_codex_mcp_config,
            )

            codex_cfg = get_config().codex
            if codex_cfg.enabled and not self.memory_only:
                mcp_servers["codex"] = build_supercc_codex_mcp_config(
                    codex_cfg,
                    cwd=self.approved_directory or ".",
                )
        except Exception:
            logger.debug("Codex MCP server was not added to Claude options", exc_info=True)

        # memory_only 模式：禁用所有内置工具，仅允许 MCP 工具（记忆相关）
        # Agent（sub-agent）也禁用，防止绕过限制启动独立执行环境
        _DISABLED_BUILTIN_TOOLS = [
            "Read", "Write", "Edit", "Bash", "Grep",
            "NotRecommend", "WebSearch", "WebFetch",
            "NotebookEdit", "TaskStart", "TaskComplete",
            "Agent",
        ]

        # 从全局 model.json 获取当前项目的激活模型 env
        # 通过 settings JSON 传递给 CLI（匹配 ~/.claude/settings.json 的结构）
        model_settings_json: str | None = None
        model_config: tuple[str, str, str] | None = None  # (auth_token, base_url, model)
        try:
            from supercc.core.models.model_config import get_model_env
            env = get_model_env()
            if env and env.ANTHROPIC_AUTH_TOKEN:
                auth_token = env.ANTHROPIC_AUTH_TOKEN
                base_url = env.ANTHROPIC_BASE_URL
                model_name = env.ANTHROPIC_MODEL or ""
                model_config = (auth_token, base_url, model_name)
                settings_obj = {
                    "env": {
                        "ANTHROPIC_AUTH_TOKEN": auth_token,
                        "ANTHROPIC_BASE_URL": base_url,
                        "ANTHROPIC_MODEL": model_name,
                    }
                }
                model_settings_json = json.dumps(settings_obj)
        except Exception as e:
            logger.error(f"Get Model Env Fail: {e}")
            pass  # 非关键路径失败不影响主流程

        # 确保 hasCompletedOnboarding=true（首次构建时调用一次，之后幂等）
        self._ensure_claude_onboarding()

        options = ClaudeAgentOptions(
            cwd=self.approved_directory or ".",
            # NOTE: 不传 cli_path，让 SDK 使用内置的 bundled CLI。
            # 显式指定 cli_path 在 Windows 上会导致 initialize() 超时。
            include_partial_messages=True,
            permission_mode="bypassPermissions",
            continue_conversation=self._continue_conversation,
            mcp_servers=mcp_servers,
            disallowed_tools=_DISABLED_BUILTIN_TOOLS if self.memory_only else [],
            settings=model_settings_json,
            session_id=session_id,
            resume=resume,
        )

        if system_prompt_append:
            options.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt_append,
            }

        self._options = options
        # 记录本次构建使用的 model 配置（下一次 _init_options 会对比是否变更）
        self._options_model_config = model_config
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
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "claude-agent-sdk is required. Install with: pip install claude-agent-sdk"
            ) from exc

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
                        # /stop 后跳过剩余 streaming 消息，避免确认打断后还有内容蹦出
                        if on_stream:
                            if self.stop_event.is_set() and msg_type != "ResultMessage":
                                continue
                            parsed = self._parse_message(message)
                            if parsed:
                                await on_stream(parsed)
                                if self._should_capture_codex(parsed):
                                    capture_task = asyncio.create_task(
                                        self._run_codex_capture(parsed, on_stream, t_query)
                                    )
                                    self._codex_capture_tasks.add(capture_task)
                                    capture_task.add_done_callback(self._codex_capture_tasks.discard)
                    return (result_text, result_session_id, result_cost)

                consume_task = asyncio.create_task(_consume())

                # Listener：监听 stop_event，收到信号时 interrupt
                async def _listener():
                    await self.stop_event.wait()
                    for task in list(self._codex_capture_tasks):
                        task.cancel()
                    await client.interrupt()
                    await consume_task
                    logger.info("[listener] stop handling done")

                listener_task = asyncio.create_task(_listener())
                try:
                    result = await consume_task
                finally:
                    listener_task.cancel()
                    if self._codex_capture_tasks:
                        await asyncio.gather(*list(self._codex_capture_tasks), return_exceptions=True)
                        self._codex_capture_tasks.clear()
            return result

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _should_capture_codex(self, msg: ClaudeMessage) -> bool:
        if msg.tool_name != "mcp__codex__codex":
            return False
        try:
            from supercc.config import get_config
            cfg = get_config()
            return (
                cfg.codex.enabled
                and cfg.codex.capture.capture_mode
                and not self.memory_only
            )
        except Exception:
            return False

    async def _run_codex_capture(
        self,
        claude_tool_msg: ClaudeMessage,
        on_stream: StreamCallback,
        started_after: float,
    ) -> None:
        try:
            payload = json.loads(claude_tool_msg.tool_input or "{}")
        except json.JSONDecodeError:
            payload = {}
        prompt = payload.get("prompt") or ""
        if not prompt:
            return
        cwd = payload.get("cwd") or self.approved_directory or "."
        runner = CodexRunTailingRunner(
            cwd=cwd,
            prompt=prompt,
            on_codex_event=on_stream,
            started_after=started_after,
            poll_interval=0.25,
            find_timeout=20,
            idle_timeout=600,
        )
        try:
            await runner.run()
        except asyncio.CancelledError:
            runner.cancel()
            raise
        except Exception:
            logger.warning("[codex_capture] failed", exc_info=True)

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

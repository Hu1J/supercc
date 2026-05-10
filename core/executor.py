"""核心消息执行器：处理 InboundMessage，调用 Claude，结果发回插件。

这是核心真正执行 AI 推理的地方。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable

from core.protocol import (
    InboundMessage, OutboundMessage,
    MessageType, Event,
)
from core.session import SessionManager
from core.worker import WorkerPool

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/[a-zA-Z][a-zA-Z0-9_-]*(?:\s.*)?$")


def _is_command(text: str) -> bool:
    return bool(_COMMAND_RE.match(text))


def _parse_command(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:]  # strip leading /
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


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
        config: Any = None,
        data_dir: str = "",
        config_path: str = "",
    ):
        self.sessions = session_manager
        self.pool = worker_pool
        self._config = config
        self._data_dir = data_dir
        self._config_path = config_path
        from core.commands.router import CommandRouter
        self._router = CommandRouter()

        # 安全组件初始化
        self._init_security(config)

    def _init_security(self, config: Any):
        """根据 config 初始化 SecurityValidator（Authenticator 按 platform 懒加载）。"""
        self._config = config
        if config is None:
            self._security_validator = None
            return

        # SecurityValidator（与平台无关，全局一份）
        claude_cfg = getattr(config, "claude", None)
        approved_dir = ""
        if claude_cfg:
            approved_dir = getattr(claude_cfg, "approved_directory", "")
        if approved_dir:
            from supercc.security.validator import SecurityValidator
            self._security_validator = SecurityValidator(approved_dir)
        else:
            self._security_validator = None

    def _get_authenticator_for_platform(self, platform: str):
        """按 platform 获取对应的 Authenticator。"""
        config = self._config
        if config is None:
            return None
        channels = getattr(config, "channels", None)
        if channels is None:
            return None
        from supercc.security.auth import Authenticator
        if platform == "wecom":
            wecom_cfg = getattr(channels, "wecom", None)
            if wecom_cfg:
                allowed = list(getattr(wecom_cfg, "allowed_users", []))
                return Authenticator(allowed) if allowed else None
        # 默认用飞书
        feishu_cfg = getattr(channels, "feishu", None)
        if feishu_cfg:
            allowed = list(getattr(feishu_cfg, "allowed_users", []))
            return Authenticator(allowed) if allowed else None
        return None

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

        # ── 安全检查 ───────────────────────────────────────────────────────

        # 1) Authenticator: P2P 白名单检查（按 platform 查找 allowed_users）
        authenticator = self._get_authenticator_for_platform(key.platform)
        if authenticator and inbound.user_open_id:
            auth_result = authenticator.authenticate(inbound.user_open_id)
            if not auth_result.authorized:
                return OutboundMessage(
                    event="command",
                    session_key=key,
                    message_id=inbound.message_id,
                    content="⛔ 抱歉，你不在允许使用列表中。",
                    message_type=MessageType.TEXT,
                )

        # 2) SecurityValidator: 内容安全检查（命令和普通消息都检查）
        if self._security_validator and inbound.content:
            ok, err_msg = self._security_validator.validate(inbound.content)
            if not ok:
                return OutboundMessage(
                    event="command",
                    session_key=key,
                    message_id=inbound.message_id,
                    content=f"⛔ 内容安全检查失败: {err_msg}",
                    message_type=MessageType.TEXT,
                )

        # ── 斜杠命令检测 ───────────────────────────────────────────────────

        if _is_command(inbound.content):
            cmd_name, cmd_args = _parse_command(inbound.content)
            context = {
                "session_key": key,
                "user_open_id": inbound.user_open_id or "",
                "chat_id": key.chat_id,
                "platform": key.platform,
                "config": self._config,
                "data_dir": self._data_dir,
                "config_path": self._config_path,
                "worker_pool": self.pool,
            }
            cmd_result = await self._router.dispatch(cmd_name, cmd_args, context)
            extra = {"card": cmd_result.card.to_dict() if cmd_result.card else None}
            extra.update(cmd_result.extra)
            return OutboundMessage(
                event=cmd_result.event,
                session_key=key,
                message_id=inbound.message_id,
                content=cmd_result.content,
                message_type=MessageType.TEXT,
                extra=extra,
            )

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

        # 执行查询（最多重试3次，SDK 空响应时重试）
        result = ""
        cost = 0.0
        for attempt in range(3):
            try:
                from supercc.claude.integration import ClaudeIntegration
                cli_path = "claude"
                if self._config and hasattr(self._config, "claude"):
                    cli_path = getattr(self._config.claude, "cli_path", "claude")
                integration = ClaudeIntegration(
                    cli_path=cli_path,
                    max_turns=50,
                    approved_directory=key.project_path,
                )

                result, cost = await self.pool.execute(
                    key=key,
                    session_id=session.session_id,
                    integration=integration,
                    prompt=prompt,
                    on_stream=_stream_callback,
                )
                if result and result.strip():
                    break  # 成功，非空
                if attempt < 2:
                    logger.info(f"[CoreExecutor] empty response, retry {attempt + 2}/3")
                    await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                logger.exception(f"[CoreExecutor] execute error for {key}")
                result = f"错误: {e}"
                cost = 0.0
                break

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
        """从 inbound 构建发送给 Claude 的 prompt，含群聊上下文。"""
        extra = inbound.extra
        parts = []

        # 群聊：注入群名、成员、引用消息、历史
        if extra.get("is_group_chat"):
            group_name = extra.get("group_name", "")
            parts.append(f"[群聊: {group_name}]")

            # 群成员列表
            members = extra.get("group_members", [])
            if members:
                member_list = ", ".join(members[:50])
                parts.append(f"[群成员] {member_list}")

            # 引用消息内容
            quoted = extra.get("quoted_content", "")
            if quoted:
                parts.append(f"[引用消息] {quoted}")

            # 群历史（最近10条）
            history = extra.get("group_history", [])
            if history:
                parts.append("[最近消息]")
                for h in history[-10:]:
                    parts.append(f"  {h}")

        parts.append(inbound.content)
        return "\n\n".join(parts)

"""核心消息执行器：处理 InboundMessage，调用 Claude，结果发回插件。

这是核心真正执行 AI 推理的地方。
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from typing import Any, Callable, Awaitable

from supercc.core.protocol import (
    InboundMessage, OutboundMessage,
    MessageType, Event,
)
from supercc.core.session import SessionManager
from supercc.core.worker import WorkerPool

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
        from supercc.core.commands.router import CommandRouter
        self._router = CommandRouter()

        # push_fn：由 server.py 在调用 execute() 时传入，用于推送 WebSocket 帧到正确连接
        # 签名: Callable[[OutboundMessage], Awaitable[None]]
        self._push_fn: Callable[[OutboundMessage], Awaitable[None]] | None = None

        # 延迟的 evolve 参数：(key, sdk_session_id, message_id, evo_ctx)
        self._pending_evolve: tuple | None = None

    def _is_verbose_enabled(self, platform: str, chat_id: str, msg_type: str) -> bool:
        """检查该 chat_id 是否开启了某类消息。默认全开。"""
        config = self._config
        if config is None:
            return True
        verbose = getattr(config, "verbose", None)
        if not verbose:
            return True
        platform_verbose = verbose.get(platform)
        if not platform_verbose:
            return True
        entry = platform_verbose.get(chat_id)
        if not entry:
            return True
        # 支持 dict 或 VerboseChannelEntry
        if isinstance(entry, dict):
            return entry.get(msg_type, True)
        return getattr(entry, msg_type, True)

    def _get_authenticator_for_platform(self, platform: str):
        """按 platform 获取对应的 Authenticator。"""
        from supercc.config import reload_config

        config = reload_config()
        if config is None:
            return None
        channels = getattr(config, "channels", None)
        if channels is None:
            return None
        from supercc.core.security.auth import Authenticator
        if platform == "wecom":
            wecom_cfg = getattr(channels, "wecom", None)
            if wecom_cfg:
                allowed = list(getattr(wecom_cfg, "allowed_users", []))
                return Authenticator(allowed) if allowed else None
        elif platform == "wechat":
            wechat_cfg = getattr(channels, "wechat", None)
            if wechat_cfg:
                allowed = list(getattr(wechat_cfg, "allowed_users", []))
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
        push_fn: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> OutboundMessage:
        """
        处理一条 InboundMessage，返回 OutboundMessage。

        on_stream: 流式输出的回调（每收到一个 chunk 调用一次）
        """
        key = inbound.session_key

        # ── MCP 工具上下文初始化（memory_tools 等依赖此获取用户身份）──────────
        from supercc.core.message_context import set_current_context
        set_current_context(
            user_open_id=inbound.user_open_id or "",
            chat_id=key.chat_id,
            platform=key.platform,
            bot_id=key.bot_id,
        )

        # ── 斜杠命令检测 ───────────────────────────────────────────────────

        if _is_command(inbound.content):
            # ── 指令权限校验：只有 allowed_users 才能执行指令 ─────────────────
            authenticator = self._get_authenticator_for_platform(key.platform)
            if authenticator and inbound.user_open_id:
                auth_result = authenticator.authenticate(inbound.user_open_id)
                if not auth_result.authorized:
                    return OutboundMessage(
                        event="command",
                        session_key=key,
                        message_id=inbound.message_id,
                        content="⛔ 抱歉，你不在允许使用指令的用户列表中。",
                        message_type=MessageType.TEXT,
                    )

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
            extra = dict(cmd_result.extra)
            # restart/update 类命令：通过 push_fn 立即把确认消息发给 plugin，
            # 让用户在重启前就能看到"正在重启..."的提示，不依赖 JSON-RPC response 的时序。
            if push_fn and cmd_result.event in ("restart", "update"):
                await push_fn(OutboundMessage(
                    event=cmd_result.event,
                    session_key=key,
                    message_id=inbound.message_id,
                    content=cmd_result.content,
                    message_type=MessageType.TEXT,
                    extra=extra,
                ))
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

        # 构建 system prompt（AGENTS.md + 系统 Guide）和用户 prompt
        system_prompt_append = self._build_system_prompt(inbound)
        prompt = self._build_prompt(inbound)

        # 流式回调包装
        accumulated = []
        _stream_too_long = [False]  # 上下文溢出标记

        # 决定用哪个回调发送 streaming 帧：优先 on_stream，否则用 push_fn
        _stream_sender = on_stream if on_stream else push_fn

        async def _stream_callback(msg: Any) -> None:
            if msg.content:
                accumulated.append(msg.content)
                logger.info("[stream] text: %s", msg.content[:200])
                # Long Context Warning：严格检测 "Prompt is too long"
                if "Prompt is too long" in msg.content:
                    _stream_too_long[0] = True
                if _stream_sender:
                    chunk = OutboundMessage(
                        event=Event.STREAM_CHUNK,
                        session_key=key,
                        message_id=inbound.message_id,
                        content=msg.content,
                        message_type=MessageType.TEXT,
                    )
                    await _stream_sender(chunk)
            elif msg.tool_name:
                logger.info("[stream] tool: %s | input: %s", msg.tool_name, (msg.tool_input or "")[:300])
                # step=OFF 时屏蔽工具调用通知，AskUserQuestion 例外始终显示
                # 工具本身由 SDK 内部执行，此处只控制是否发 TOOL_CALL WS 事件给 plugin
                if _stream_sender and (self._is_verbose_enabled(key.platform, key.chat_id, "step") or msg.tool_name == "AskUserQuestion"):
                    tool_msg = OutboundMessage(
                        event=Event.TOOL_CALL,
                        session_key=key,
                        message_id=inbound.message_id,
                        content=f"[{msg.tool_name}]",
                        message_type=MessageType.TOOL_CALL,
                        extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                    )
                    await _stream_sender(tool_msg)

        async def _on_start() -> None:
            """拿到锁后、开始处理前触发，通知 plugin 可以打 typing OK 了。"""
            if push_fn:
                await push_fn(OutboundMessage(
                    event=Event.PROCESSING,
                    session_key=key,
                    message_id=inbound.message_id,
                    content="",
                    message_type=MessageType.TEXT,
                ))

        # 执行查询（最多重试3次，SDK 空响应时重试）
        result = ""
        cost = 0.0
        cli_path = "claude"
        if self._config and hasattr(self._config, "claude"):
            cli_path = getattr(self._config.claude, "cli_path", "claude")

        # 记录旧的 SDK session ID，用于检测 session 切换
        old_sdk_sid = session.sdk_session_id
        new_sdk_sid = None
        for attempt in range(3):
            try:
                result, cost, sdk_sid = await self.pool.execute(
                    key=key,
                    session_id=session.session_id,
                    prompt=prompt,
                    system_prompt_append=system_prompt_append,
                    cli_path=cli_path,
                    approved_dir=key.project_path,
                    on_stream=_stream_callback,
                    on_start=_on_start,
                    sdk_session_id=session.sdk_session_id,
                )
                new_sdk_sid = sdk_sid
                if result and result.strip():
                    break  # 成功，非空
                if attempt < 2:
                    logger.info(f"[CoreExecutor] empty response, retry {attempt + 2}/3")
                    await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                logger.error("[CoreExecutor] execute error for %s\n%s", key, traceback.format_exc())
                result = f"""
---

⚠️ **系统执行错误**

`{type(e).__name__}: {e}`
"""
                cost = 0.0
                break

        # ── SDK Session 切换检测 ──────────────────────────────────────────
        session_info = ""
        if new_sdk_sid and new_sdk_sid != old_sdk_sid:
            self.sessions.update_sdk_session_id(session.session_id, new_sdk_sid)
            if old_sdk_sid:
                session_info = f"🔄 已切换到新 Session\nSession ID: `{new_sdk_sid}`"
            else:
                session_info = f"✅ 新 Session 已建立\nSession ID: `{new_sdk_sid}`"
                logger.info(f"[CoreExecutor] new SDK session established: {new_sdk_sid} for {key}")

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

        # ── 群聊 sender 信息（plugin 层自行判断是否追加 mention）────────────
        # core 只传 sender 信息，不生成平台特有格式；各 plugin 自主选择原生 mention 格式。
        sender_id = inbound.user_open_id or ""
        sender_name = ""
        if inbound.extra.get("is_group_chat") and sender_id:
            members = inbound.extra.get("group_members", [])
            for m in members:
                if isinstance(m, dict):
                    member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                    name = m.get("name") or m.get("bot_name", "")
                else:
                    member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", "")
                    name = getattr(m, "name", None) or ""
                if member_id == sender_id and name:
                    sender_name = name
                    break

        # ── 发送主响应 + 触发后台任务 ────────────────────────────────────
        extra_dict = {
            "user_open_id": sender_id,
            "sender_name": sender_name,
            "is_group_chat": inbound.extra.get("is_group_chat", False),
            "group_members": inbound.extra.get("group_members", []),
        }
        if session_info:
            extra_dict["session_info"] = session_info

        result_msg = OutboundMessage(
            event=Event.RESPONSE,
            session_key=key,
            message_id=inbound.message_id,
            content=result,
            message_type=MessageType.TEXT,
            extra=extra_dict,
        )

        # 通过 push_fn 发送主响应（server.py 通过此机制推送 WebSocket 帧）
        if push_fn:
            self._push_fn = push_fn
            await push_fn(result_msg)
            # 触发自进化（异步，不阻塞主响应返回）
            sdk_sid = new_sdk_sid or ""
            from supercc.core.message_context import get_current_user_open_id, get_current_chat_id, get_current_platform, get_current_bot_id
            evo_ctx = {
                "user_open_id": get_current_user_open_id() or "",
                "chat_id": get_current_chat_id() or "",
                "platform": get_current_platform(),
                "bot_id": get_current_bot_id() or "",
            }
            self._pending_evolve = (key, sdk_sid, inbound.message_id, evo_ctx)

        # 上下文超限提示：检测到 "Prompt is too long" 后主动发消息
        if _stream_too_long[0] and push_fn:
            too_long_msg = OutboundMessage(
                event=Event.NOTIFICATION,
                session_key=key,
                message_id=inbound.message_id,
                content="当前上下文已满，请发 /new 指令开启新对话",
                message_type=MessageType.TEXT,
            )
            await push_fn(too_long_msg)

        return result_msg

    def _build_system_prompt(self, inbound: InboundMessage) -> str:
        """构建 system prompt（Claude 系统指令），含 AGENTS.md 和系统 Guide。"""
        key = inbound.session_key
        parts = []

        # ── AGENTS.md ─────────────────────────────────────────────────────
        if key.project_path:
            import os
            agents_md_path = os.path.join(key.project_path, "AGENTS.md")
            if os.path.isfile(agents_md_path):
                try:
                    with open(agents_md_path, encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        parts.append(content)
                except Exception:
                    pass

        # ── 系统 Guide ──────────────────────────────────────────────────
        system_parts = []

        try:
            from supercc.core.memory_manager import MEMORY_SYSTEM_GUIDE
            system_parts.append(MEMORY_SYSTEM_GUIDE)
        except Exception:
            pass

        if key.platform == "feishu":
            try:
                from supercc.core.mcps.feishu_file_tools import FEISHU_FILE_GUIDE
                system_parts.append(FEISHU_FILE_GUIDE)
            except Exception:
                pass
        elif key.platform == "wecom":
            try:
                from supercc.core.mcps.wecom_tools import WECOM_FILE_GUIDE
                system_parts.append(WECOM_FILE_GUIDE)
            except Exception:
                pass

        try:
            from supercc.core.mcps.cron_tools import CRON_GUIDE
            system_parts.append(CRON_GUIDE)
        except Exception:
            pass

        # Memory Context Injection
        try:
            from supercc.core.memory_manager import get_memory_manager
            memory_manager = get_memory_manager()
            memory_ctx = memory_manager.inject_context(
                user_open_id=inbound.user_open_id or "",
                project_path=key.project_path,
                platform=key.platform,
                chat_id=key.chat_id,
                bot_id=key.bot_id,
            )
            if memory_ctx:
                system_parts.append(memory_ctx)
        except Exception:
            pass

        if system_parts:
            parts.append("\n".join(system_parts))

        # 插件注入的系统级指令（追加到末尾）
        if inbound.system_prompt:
            parts.append(inbound.system_prompt)

        return "\n\n".join(parts)

    def _build_prompt(self, inbound: InboundMessage) -> str:
        """构建用户 prompt。

        非指令且群聊上下文非空时，将 group_context 前置到 content，
        让 Claude 直接在对话上下文中看到历史消息。
        """
        if inbound.group_context and not _is_command(inbound.content):
            return inbound.group_context + "\n\n" + inbound.content
        return inbound.content

    def flush_evolve(self) -> None:
        """在 WS 响应发送后触发延迟的 evolve 任务。"""
        pending = self._pending_evolve
        self._pending_evolve = None
        if pending:
            key, sdk_sid, msg_id, evo_ctx = pending
            asyncio.create_task(self._run_evolve(key, sdk_sid, msg_id, evo_ctx))

    async def _run_evolve(self, key: SessionKey, sdk_session_id: str, message_id: str = "", evo_ctx: dict | None = None) -> None:
        from supercc.core.evolve.evolve import run_evolve
        try:
            worker = await self.pool.get(key)
        except Exception:
            return

        await run_evolve(
            worker=worker,
            pool=self.pool,
            key=key,
            sdk_session_id=sdk_session_id,
            message_id=message_id,
            evo_context=evo_ctx,
            data_dir=self._data_dir,
            push_fn=self._push_fn,
            is_verbose_enabled_fn=self._is_verbose_enabled,
            config=self._config,
            _logger=logger,
        )


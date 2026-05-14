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

# 技能自进化触发阈值：累计 tool_call 达到此数量时触发
SKILL_NUDGE_THRESHOLD = 10


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

        # 安全组件初始化
        self._init_security(config)

        # push_fn：由 server.py 在调用 execute() 时传入，用于推送 WebSocket 帧到正确连接
        # 签名: Callable[[OutboundMessage], Awaitable[None]]
        self._push_fn: Callable[[OutboundMessage], Awaitable[None]] | None = None

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
        return getattr(entry, msg_type, True)

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
        push_fn: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> OutboundMessage:
        """
        处理一条 InboundMessage，返回 OutboundMessage。

        on_stream: 流式输出的回调（每收到一个 chunk 调用一次）
        """
        key = inbound.session_key

        # ── MCP 工具上下文初始化（memory_tools 等依赖此获取用户身份）──────────
        from supercc.claude.message_context import set_current_context
        set_current_context(
            user_open_id=inbound.user_open_id or "",
            chat_id=key.chat_id,
            platform=key.platform,
        )

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

        # 构建 system prompt（AGENTS.md + 系统 Guide）和用户 prompt
        system_prompt_append = self._build_system_prompt(inbound)
        prompt = self._build_prompt(inbound)

        # 流式回调包装
        accumulated = []
        _tool_count = 0  # 本次查询的 tool call 计数
        _stream_too_long = [False]  # 上下文溢出标记

        # 决定用哪个回调发送 streaming 帧：优先 on_stream，否则用 push_fn
        _stream_sender = on_stream if on_stream else push_fn

        async def _stream_callback(msg: Any) -> None:
            nonlocal _tool_count
            if msg.content:
                accumulated.append(msg.content)
                logger.info("[stream] text: %s", msg.content[:200])
                # Long Context Warning：检测上下文溢出关键词
                content_lower = msg.content.lower()
                if (
                    "too long" in content_lower
                    or "超出" in msg.content
                    or "context window" in content_lower
                    or "context_length" in content_lower
                    or "max_tokens" in content_lower
                ):
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
                nonlocal _tool_count
                _tool_count += 1
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
                )
                new_sdk_sid = sdk_sid
                if result and result.strip():
                    break  # 成功，非空
                if attempt < 2:
                    logger.info(f"[CoreExecutor] empty response, retry {attempt + 2}/3")
                    await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                logger.error("[CoreExecutor] execute error for %s\n%s", key, traceback.format_exc())
                result = f"错误: {e}"
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

        # 累加工具调用计数到 Worker（供技能自进化阈值判断）
        total_tool_count = 0
        if _tool_count > 0:
            try:
                worker = await self.pool.get(key)
                if worker:
                    worker.tool_call_count += _tool_count
                    total_tool_count = worker.tool_call_count
            except Exception:
                pass

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

        # ── 群聊 @mention 检查：若 AI 未 mention 提问者，追加 ────────────
        # 核心只计算 mention_tag，放在 extra 中传给 plugin；
        # plugin 在 RESPONSE 事件到达时追加到最后的流式消息，避免重复发送完整内容。
        mention_tag = ""
        if key.platform == "feishu" and inbound.extra.get("is_group_chat"):
            members = inbound.extra.get("group_members", [])
            sender_id = inbound.user_open_id or ""
            if sender_id and members:
                for m in members:
                    if isinstance(m, dict):
                        member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                        name = m.get("name") or m.get("bot_name", "")
                    else:
                        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", "")
                        name = getattr(m, "name", None) or ""
                    if member_id == sender_id and name:
                        mention_tag = f"\n<at user_id=\"{member_id}\">{name}</at>"
                        break
                # 如果 AI 已自然 mention，mention_tag 保持为空
                if mention_tag and f'<at user_id="{sender_id}"' in result:
                    mention_tag = ""

        # ── 发送主响应 + 触发后台任务 ────────────────────────────────────
        extra_dict = {
            "mention_tag": mention_tag,
            "user_open_id": inbound.user_open_id or "",
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
            # 触发后台任务（异步，不阻塞主响应返回）
            # 用原始 result 作为记忆回顾的上下文
            asyncio.create_task(self._run_background_tasks(key, prompt, total_tool_count, inbound.message_id, inbound.user_open_id or ""))

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
            from supercc.claude.memory_manager import MEMORY_SYSTEM_GUIDE
            system_parts.append(MEMORY_SYSTEM_GUIDE)
        except Exception:
            pass

        try:
            from supercc.claude.feishu_file_tools import FEISHU_FILE_GUIDE
            system_parts.append(FEISHU_FILE_GUIDE)
        except Exception:
            pass

        try:
            from supercc.claude.cron_tools import CRON_GUIDE
            system_parts.append(CRON_GUIDE)
        except Exception:
            pass

        # Memory Context Injection
        try:
            from supercc.claude.memory_manager import get_memory_manager
            memory_manager = get_memory_manager()
            memory_ctx = memory_manager.inject_context(
                user_open_id=inbound.user_open_id or "",
                project_path=key.project_path,
                platform=key.platform,
                chat_id=key.chat_id,
            )
            if memory_ctx:
                system_parts.append(memory_ctx)
        except Exception:
            pass

        if system_parts:
            parts.append("\n".join(system_parts))

        return "\n\n".join(parts)

    def _build_prompt(self, inbound: InboundMessage) -> str:
        """从 inbound 构建用户 prompt（群聊上下文 + 用户消息）。"""
        extra = inbound.extra
        parts = []

        # ── @mention 规则 ────────────────────────────────────────────────
        mention_rules = extra.get("mention_rules", "")
        if mention_rules:
            parts.append(mention_rules)

        # ── 群聊上下文 ─────────────────────────────────────────────────
        if extra.get("is_group_chat"):
            group_name = extra.get("group_name", "")
            parts.append(f"[群聊: {group_name}]")

            members = extra.get("group_members", [])
            if members:
                member_lines = [
                    "【群聊规则】必须在最终回复里艾特@{发送者}以及相关人员。"
                    "使用飞书 @ 格式如：<at user_id=\"open_id\">姓名</at>。不得遗漏。"
                ]
                for m in members[:50]:
                    if isinstance(m, dict):
                        member_id = m.get("member_id") or m.get("open_id") or m.get("bot_id", "")
                        name = m.get("name") or m.get("bot_name", "")
                    else:
                        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or getattr(m, "bot_id", "")
                        name = getattr(m, "name", None) or ""
                    if member_id and name:
                        member_lines.append(f"  {name}: <at user_id=\"{member_id}\">{name}</at>")
                parts.append("\n".join(member_lines))

            quoted = extra.get("quoted_content", "")
            if quoted:
                parts.append(f"[引用消息] {quoted}")

            history = extra.get("group_history", [])
            if history:
                parts.append("[最近消息]")
                for h in history[-10:]:
                    parts.append(f"  {h}")

        # ── 用户消息 ────────────────────────────────────────────────────
        parts.append(inbound.content)
        return "\n\n".join(parts)

    async def _run_background_tasks(self, key: SessionKey, prompt: str, total_tool_count: int = 0, message_id: str = "", user_open_id: str = "") -> None:
        """触发记忆自进化（integration_mem）和技能自进化（integration_skill）。

        由 executor.execute() 在主响应发送后异步调用，不阻塞主响应返回。
        结果通过 self._push_fn 推送回 plugin（WebSocket）。
        """
        push_fn = self._push_fn
        if not push_fn:
            return

        try:
            worker = await self.pool.get(key)
        except Exception:
            return

        if worker is None:
            return

        # ── MCP 工具上下文（background task 中 memory MCP 工具也依赖此）────────
        from supercc.claude.message_context import set_current_context
        set_current_context(user_open_id=user_open_id, chat_id=key.chat_id, platform=key.platform)

        # ── 记忆自进化（只推 memory MCP 工具卡片，不推文本流）───────────
        mem_enabled = self._is_verbose_enabled(key.platform, key.chat_id, "mem")
        if worker.integration_mem:
            try:
                worker.integration_mem._init_options(channel=key.platform)
                memory_prompt = (
                    "根据之前的对话，判断是否有值得记住的信息。\n"
                    "需要时直接调用 MCP 工具（新增/更新/删除）来管理记忆，"
                    "不需要问我任何问题。\n"
                    "本次对话内容参考：\n" + prompt[-1000:]
                )

                async def mem_stream_callback(msg: Any) -> None:
                    # 日志打印 AI 说的内容
                    if msg.content:
                        logger.info("[Background] memory review: %s", msg.content[:500])
                    # 只推 memory MCP 工具调用（卡片），不推文本流
                    if msg.tool_name and push_fn and mem_enabled:
                        tool_msg = OutboundMessage(
                            event=Event.TOOL_CALL,
                            session_key=key,
                            message_id=message_id,
                            content=f"[{msg.tool_name}]",
                            message_type=MessageType.TOOL_CALL,
                            extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                        )
                        await push_fn(tool_msg)

                await worker.integration_mem.query(
                    prompt=memory_prompt,
                    on_stream=mem_stream_callback,
                )
                logger.info(f"[Background] memory review done for {key}")
            except Exception as e:
                logger.warning(f"[Background] memory review failed: {e}")

        # ── 技能自进化（检查 skills git 变更，有变更才通知）───────────
        skill_enabled = self._is_verbose_enabled(key.platform, key.chat_id, "skill")
        if worker.integration_skill and total_tool_count >= SKILL_NUDGE_THRESHOLD:
            try:
                from pathlib import Path
                from supercc.evolve.skill_nudge import _get_skill_git_state

                worker.integration_skill._init_options(channel=key.platform)

                skills_dir = Path(self._data_dir) / "skills"

                # 快照当前的 skills git 状态（执行前）
                before_state = _get_skill_git_state(skills_dir)

                skill_prompt = (
                    f"你在本次对话中使用了 {total_tool_count} 个工具调用。\n"
                    "请分析这些工具调用的模式，判断是否有可以优化或封装成技能的常见工作流。\n"
                    "如果发现值得技能化的模式，直接调用 MCP 工具来创建或更新技能，"
                    "不需要问我任何问题。"
                )

                # 空的 stream callback：不推送中间过程
                async def skill_stream_callback(msg: Any) -> None:
                    pass

                await worker.integration_skill.query(
                    prompt=skill_prompt,
                    on_stream=skill_stream_callback,
                )

                # 检查 skills git 状态是否有变更
                from supercc.evolve.skill_nudge import _detect_skill_changes

                if skill_enabled:
                    # 把 push_fn 包装为 (chat_id, text) 签名供 _detect_skill_changes 调用
                    async def _send_skill_notify(cid: str, text: str) -> None:
                        result_msg = OutboundMessage(
                            event=Event.RESPONSE,
                            session_key=key,
                            message_id=message_id,
                            content=text,
                            message_type=MessageType.TEXT,
                            extra={},
                        )
                        await push_fn(result_msg)

                    await _detect_skill_changes(
                        before_state=before_state,
                        skills_dir=skills_dir,
                        chat_id=key.chat_id,
                        send_to_feishu=_send_skill_notify,
                        notify=True,
                    )
                else:
                    # skill=OFF 时只检测不推送
                    await _detect_skill_changes(
                        before_state=before_state,
                        skills_dir=skills_dir,
                        notify=False,
                    )

                logger.info(f"[Background] skill review done for {key} ({total_tool_count} tool calls)")
            except Exception as e:
                logger.warning(f"[Background] skill review failed: {e}")


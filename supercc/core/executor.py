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
            asyncio.create_task(self._run_evolve(key, sdk_sid, inbound.message_id, inbound.user_open_id or ""))

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

    async def _run_evolve(self, key: SessionKey, sdk_session_id: str, message_id: str = "", user_open_id: str = "") -> None:
        """自进化：分析 session 文件，先做记忆处理，再做技能处理。

        由 executor.execute() 在主响应发送后异步调用，不阻塞主响应返回。
        结果通过 self._push_fn 推送回 plugin（WebSocket）。
        """
        push_fn = self._push_fn
        if not push_fn or not sdk_session_id:
            return

        try:
            worker = await self.pool.get(key)
        except Exception:
            return

        if worker is None or worker.integration_evolve is None:
            return

        # ── 找 session 文件 ────────────────────────────────────────────────
        from pathlib import Path
        evo_logger = logger

        session_path = None
        try:
            for f in Path.home().rglob("*.jsonl"):
                if f.name == f"{sdk_session_id}.jsonl":
                    session_path = str(f)
                    break
        except Exception:
            pass

        if not session_path:
            evo_logger.warning(f"[evolve] session file not found for {sdk_session_id}, skipping")
            return

        # ── MCP 工具上下文 ───────────────────────────────────────────────
        from supercc.core.message_context import set_current_context
        set_current_context(user_open_id=user_open_id, chat_id=key.chat_id, platform=key.platform, bot_id=key.bot_id)

        # ── 构造 evolve prompt ──────────────────────────────────────────
        skills_dir = Path(self._data_dir) / "skills"
        evolve_prompt = f"""分析 session 文件：{session_path}

这是一个 JSONL 格式的对话记录，每行一条 JSON。文件末尾就是最近一次完整对话。

请严格按以下两阶段依次执行，不准跳过任何阶段：

===== 阶段一：记忆自进化 =====

先阅读最近一次完整对话（文件末尾）：
1. 用户最后说了什么？
2. 用了哪些工具（Read/Write/Edit/Bash/Grep/WebSearch 等），传了什么参数？
3. 工具返回了什么结果？
4. Claude 最终回复了什么？

如果最近一次对话信息不够判断，再往前追溯更早的消息。

判断以下事项，需要时直接调 MCP 工具执行，不要问我任何问题：
- 用户的个人偏好（语言风格、沟通习惯、技术栈偏好等）
  → mcp__SuperCC__MemoryAddUser / MemoryUpdateUser / MemoryDeleteUser
- 项目相关的记忆（文件路径、代码规范、bug 修复、架构决策等）
  → mcp__SuperCC__MemoryAddProj / MemoryUpdateProj / MemoryDeleteProj

===== 阶段二：技能自进化 =====

先查看 {skills_dir}/ 目录，其中每个子目录对应一个技能（如 {skills_dir}/技能A/），
每个技能目录内包含 SKILL.md 文件。

然后分析本次对话全程，判断：

**符合构建 Skill 的条件：**
- 流程、模式固定的重复性工作流（每次做同样步骤的那种）
- 充当辅助工具来简化某个操作流程的工具型 Skill
- **特别是需要写工具代码（Bash 脚本、代码片段等）来辅助的场景**

**不符合的条件：**
- 一次性任务，没有重复价值
- 纯记忆类信息（应该存为记忆，不是 Skill）

同时检查已有 Skill 是否有过时或需要更新的内容，以及是否有多个 Skill 可以合并。

**SKILL.md 元数据格式（YAML frontmatter）：**
```yaml
---
name: skill-name
description: 简短描述
version: 1.0
usage_count: 0       # 使用次数，每被成功执行一次 +1
status: active       # active / archived
created_at: 2026-01-01
updated_at: 2026-01-01
---
```

**每个新建或更新 Skill 时，必须在正文末尾追加以下使用追踪指令（固定文案）：**
```
> **使用追踪**：每次你读取此 SKILL.md 并成功按指示执行后，
> 请将 frontmatter 的 `usage_count` +1、更新 `updated_at` 为当天日期。
> 如果是更新 Skill 内容，只更新 `updated_at`，不改 `usage_count`。
```

操作规则：
- **新建**：在 {skills_dir}/ 下创建 <技能名>/ 目录，写入 SKILL.md（完整 frontmatter + 使用追踪指令）
- **更新**：修改已有 SKILL.md 的正文，不要动 frontmatter 的 name/description
- **删除**：删除 {skills_dir}/<技能名>/ 整个目录（含 SKILL.md）
- 写入或更新后，执行：
  ```
  cd {skills_dir} && git add <技能名>/ && git commit -m "<中文 commit message>"
  ```
  不要 git push（此仓库没有 remote）

**删除条件：同时满足以下三条才删，满足时直接删不需要问：**
1. usage_count 长时间为 0（超过一个月没有使用）
2. 内容过时、有错误、或已被新 Skill 替代
3. 评估后认为对当前项目确实已无价值

不满足上述条件但认为有疑问的，用 AskUserQuestion 问用户确认。

每次操作完成后，同步更新项目记忆：
- 新建技能 → MemoryAddProj(title="skill:<技能名>", content="简短描述/用法", keywords="skill,<技能名>")
- 更新技能 → MemoryUpdateProj(...)
- 删除技能 → MemoryDeleteProj(...)

**补全规则：** 已有 Skill 缺少 usage tracking 元数据时，自动补全 frontmatter（加 usage_count/status/created_at/updated_at）和正文末尾的使用追踪指令。

注意：
- 新建前先搜索记忆确认不重复
- 记忆中已有相关描述时，不要再创建冗余的 Skill
- 只创建真正有价值的 Skill，不要为"有"而创建"""

        # ── 执行 evolve ─────────────────────────────────────────────────────
        is_evo_verbose = self._is_verbose_enabled(key.platform, key.chat_id, "evolve")
        try:
            worker.integration_evolve._init_options(channel=key.platform, continue_conversation=False)

            async def evolve_stream_callback(msg: Any) -> None:
                if msg.content:
                    evo_logger.info("[evolve] text: %s", msg.content[:500])
                if not is_evo_verbose:
                    return
                if msg.tool_name and push_fn:
                    tool_msg = OutboundMessage(
                        event=Event.TOOL_CALL,
                        session_key=key,
                        message_id=message_id,
                        content=f"[{msg.tool_name}]",
                        message_type=MessageType.TOOL_CALL,
                        extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                    )
                    await push_fn(tool_msg)

            await worker.integration_evolve.query(
                prompt=evolve_prompt,
                on_stream=evolve_stream_callback,
            )
            evo_logger.info(f"[evolve] done for {key}")
        except Exception as e:
            evo_logger.warning(f"[evolve] failed: {e}")


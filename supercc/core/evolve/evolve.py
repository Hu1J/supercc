"""Evo — 自进化核心逻辑（独立于 executor）。

每次对话后触发，分析 session 文件，驱动记忆和技能自进化。
阶段一（每次）：读对话 → 判断新增记忆
阶段二（每5次）：全面巡检记忆 + 技能自进化
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from supercc.core.protocol import SessionKey

logger = logging.getLogger(__name__)

# ── Sandbox 白名单 ────────────────────────────────────────────────────────────

MCP_MEMORY_TOOLS = [
    "mcp__SuperCC__MemoryListUser",
    "mcp__SuperCC__MemoryListProj",
    "mcp__SuperCC__MemorySearchUser",
    "mcp__SuperCC__MemorySearchProj",
    "mcp__SuperCC__MemoryAddUser",
    "mcp__SuperCC__MemoryUpdateUser",
    "mcp__SuperCC__MemoryDeleteUser",
    "mcp__SuperCC__MemoryAddProj",
    "mcp__SuperCC__MemoryUpdateProj",
    "mcp__SuperCC__MemoryDeleteProj",
]


def _build_permissions_allow(skills_dir: str) -> list[dict]:
    return [
        {"tool": "Read", "path": "**"},
        {"tool": "Edit", "path": f"{skills_dir}/**"},
        {"tool": "Write", "path": f"{skills_dir}/**"},
        {"tool": "Bash", "path": f"{skills_dir}/**"},
        *({"tool": t} for t in MCP_MEMORY_TOOLS),
    ]


# ── Prompt 构造 ──────────────────────────────────────────────────────────────

def build_evolve_prompt(
    session_path: str,
    project_path: str,
    skills_dir: str,
    do_full: bool,
) -> str:
    """构造完整 evolve prompt。do_full=True 时包含阶段二（全面巡检+技能自进化）。"""
    phase1 = f"""项目路径：{project_path}
session 文件：{session_path}

这是一个 JSONL 格式的对话记录，每行一条 JSON。文件末尾就是最近一次完整对话。

===== 阶段一：记忆自进化 =====

先阅读最近一次完整对话（文件末尾）：
1. 用户最后说了什么？
2. 用了哪些工具（Read/Write/Edit/Bash/Grep/WebSearch 等），传了什么参数？
3. 工具返回了什么结果？
4. Claude 最终回复了什么？

如果最近一次对话信息不够判断，再往前追溯更早的消息。

**根据最新对话判断是否有新增记忆：**
- 用户偏好类（语言风格、沟通习惯、技术栈偏好）→ mcp__SuperCC__MemoryAddUser / MemoryUpdateUser / MemoryDeleteUser
- 项目记忆类（文件路径、代码规范、bug、架构决策）→ mcp__SuperCC__MemoryAddProj / MemoryUpdateProj / MemoryDeleteProj

如有新增，直接调用 MCP 工具执行，完成后输出简短报告。"""

    phase2 = f"""

===== 阶段二：记忆全面巡检 =====

先对记忆库做全面巡检：
1. 调用 `mcp__SuperCC__MemoryListUser` 获取所有用户偏好
2. 对 project_path 调用 `mcp__SuperCC__MemoryListProj` 获取所有项目记忆

**全面巡检任务：**
- **纠正放错位置的记忆**：用户偏好中出现项目特有信息（文件路径、代码规范、bug、架构决策、git）或项目记忆中出现用户个人偏好（语言风格、沟通习惯），说明放错位置了 → 先在正确位置 MemoryAdd，再 MemoryDelete 旧的
- **合并重复**：内容高度相似的记忆，合并为一条最完整的
- **精简冗长**：啰嗦的记忆内容去掉重复表述，保留关键信息
- **删除过时**：已无价值或严重过时的记忆，用 MemoryDelete 删除

===== 阶段三：技能自进化 =====

先查看 {skills_dir}/ 目录，其中每个子目录对应一个技能（如 {skills_dir}/技能A/），
每个技能目录内包含 SKILL.md 文件。

然后分析本次对话全程，判断：

**符合构建 Skill 的条件：**
- 流程、模式固定的重复性工作流（每次做同样步骤的那种）
- 充当辅助工具来简化某个操作流程的工具型 Skill
- **特别是需要写工具代码（Bash 脚本、代码片段等）来辅助的场景**

**不符合的条件：**
- 一次性任务，没有重复价值
- 规则、规范、架构设计等静态参考信息（应该存为项目记忆，不是 Skill）
- 纯记忆类信息（应该存为用户偏好或项目记忆，不是 Skill）

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

    return phase1 + (phase2 if do_full else "")


# ── 找 session 文件 ───────────────────────────────────────────────────────────

def find_session_path(sdk_session_id: str) -> str | None:
    """根据 SDK session ID 查找对应的 .jsonl 文件路径。"""
    try:
        for f in Path.home().rglob("*.jsonl"):
            if f.name == f"{sdk_session_id}.jsonl":
                return str(f)
    except Exception:
        pass
    return None


# ── 主逻辑 ───────────────────────────────────────────────────────────────────

async def run_evolve(
    worker: Any,
    key: SessionKey,
    sdk_session_id: str,
    message_id: str,
    user_open_id: str,
    data_dir: str,
    push_fn: Callable[[Any], Any] | None,
    is_verbose_enabled_fn: Callable[[str, str, str], bool],
    config: Any,
    _logger: logging.Logger | None = None,
) -> None:
    """自进化主逻辑。

    - 每次对话后：只执行阶段一（记忆自进化）
    - 每 5 次对话后：执行完整两阶段

    evolve 使用独立的 SDK session（与主对话完全隔离），支持 resume 续接。
    由 executor.execute() 在主响应发送后异步调用，不阻塞主响应返回。
    """
    evo_logger = _logger or logger

    if not push_fn or not sdk_session_id:
        return

    if worker is None or worker.integration_evolve is None:
        return

    # ── 决定执行哪个阶段 ─────────────────────────────────────────────
    evo_count = worker._evo_conversation_count
    do_full = evo_count >= 5
    evo_logger.info(f"[evolve] count={evo_count}, full={'yes' if do_full else 'no'}")

    # ── 找 session 文件 ────────────────────────────────────────────────
    session_path = find_session_path(sdk_session_id)
    if not session_path:
        evo_logger.warning(f"[evolve] session file not found for {sdk_session_id}, skipping")
        return

    # ── MCP 工具上下文 ───────────────────────────────────────────────
    from supercc.core.message_context import set_current_context, get_current_bot_id, get_current_user_open_id
    set_current_context(
        user_open_id=user_open_id,
        chat_id=key.chat_id,
        platform=key.platform,
        bot_id=key.bot_id,
    )
    evo_logger.debug(f"[evolve] context set: bot_id={key.bot_id}, user_open_id={user_open_id}, chat_id={key.chat_id}, platform={key.platform}, actual_bot_id={get_current_bot_id()}, actual_user={get_current_user_open_id()}")

    skills_dir = str(Path(data_dir) / "skills")

    # ── 构造 prompt ──────────────────────────────────────────────────
    full_prompt = build_evolve_prompt(
        session_path=session_path,
        project_path=key.project_path,
        skills_dir=skills_dir,
        do_full=do_full,
    )

    # ── 执行 evolve ───────────────────────────────────────────────────────
    is_evo_verbose = is_verbose_enabled_fn(key.platform, key.chat_id, "evolve")

    # 技能变更通知：full evo 前后对比 git state
    from supercc.core.evolve.skill_nudge import _detect_skill_changes, _get_skill_git_state
    before_skill_state: dict[str, str | None] = {}
    if do_full:
        before_skill_state = _get_skill_git_state(Path(skills_dir))

    async def _skill_notify_wrapper(chat_id: str, text: str) -> None:
        """将 push_fn 包装为 send_to_feishu(chat_id, text) 签名。"""
        if not push_fn:
            return
        from supercc.core.protocol import Event, MessageType, OutboundMessage
        import uuid
        notify_msg = OutboundMessage(
            event=Event.NOTIFICATION,
            session_key=key,
            message_id=str(uuid.uuid4()),
            content=text,
            message_type=MessageType.TEXT,
        )
        await push_fn(notify_msg)

    try:
        worker.integration_evolve.approved_directory = skills_dir

        evo_resume = worker._sdk_session_id_evolve
        worker.integration_evolve._init_options(
            channel=key.platform,
            continue_conversation=False,
            session_id=None,
            resume=evo_resume,
        )
        if worker.integration_evolve._options is not None:
            opts = worker.integration_evolve._options
            opts.permission_mode = "bypassPermissions"
            opts.sandbox = {"enabled": True, "excludedCommands": ["git"]}

        async def evolve_stream_callback(msg: Any) -> None:
            if msg.content:
                evo_logger.info("[evolve] text: %s", msg.content[:500])
            if not is_evo_verbose:
                return
            if msg.tool_name and msg.tool_name.startswith("mcp__SuperCC__Memory") and push_fn:
                from supercc.core.protocol import Event, MessageType, OutboundMessage
                tool_msg = OutboundMessage(
                    event=Event.TOOL_CALL,
                    session_key=key,
                    message_id=message_id,
                    content=f"[{msg.tool_name}]",
                    message_type=MessageType.TOOL_CALL,
                    extra={"tool_name": msg.tool_name, "tool_input": msg.tool_input},
                )
                await push_fn(tool_msg)

        _, evo_sid, _ = await worker.integration_evolve.query(
            prompt=full_prompt,
            on_stream=evolve_stream_callback,
        )
        if evo_sid:
            worker._sdk_session_id_evolve = evo_sid
            evo_logger.info(f"[evolve] SDK session: {evo_sid}, full={do_full}")

        # 技能变更通知（full evo 结束后检测并推送，受 /verbose evolve on|off 控制）
        if do_full and is_evo_verbose:
            await _detect_skill_changes(
                before_state=before_skill_state,
                skills_dir=Path(skills_dir),
                chat_id=key.chat_id,
                send_to_feishu=_skill_notify_wrapper,
                notify=True,
            )

        if do_full:
            worker._evo_conversation_count = 0

        evo_logger.info(f"[evolve] done for {key} (full={do_full})")
    except Exception as e:
        evo_logger.warning(f"[evolve] failed: {e}")


def cleanup_old_cron_jobs(data_dir: str) -> None:
    """启动时删除旧版的「做梦」和「Skill 优化扫描」定时任务。"""
    from supercc.core.cron_scheduler import list_jobs, delete_job

    old_names = {"做梦", "Skill 优化扫描"}
    try:
        jobs = list_jobs(data_dir)
        for j in jobs:
            if j.get("name") in old_names:
                delete_job(j["id"], data_dir)
                logger.debug(f"[evolve] cleaned up old cron job: {j.get('name')}")
    except Exception:
        pass

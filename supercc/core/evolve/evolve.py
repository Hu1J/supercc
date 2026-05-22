"""Evo — 自进化核心逻辑（独立于 executor）。

三种模式：
- 印象快照（incremental_memory）：每次对话后增量记忆（bypassPermissions + MCP-only）
- 记忆精炼（memory_inspection）：巡检时全面记忆巡检（bypassPermissions + MCP-only）
- 技能巡检（skill_audit）：巡检时技能自进化（auto + can_use_tool 路径限制）

每次对话后必做印象快照；满足巡检条件时顺序执行印象快照 → 技能巡检 → 记忆精炼，
三者在同一个 SDK session 中接续执行，上下文无缝衔接。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable

from supercc.core.protocol import SessionKey

logger = logging.getLogger(__name__)

# ── Prompt 构造 ──────────────────────────────────────────────────────────────

def build_evolve_prompt(
    session_path: str,
    project_path: str,
    skills_dir: str,
    mode: str,
) -> str:
    """三种模式的 prompt：
    incremental_memory：增量记忆（从本次对话提取新增/变更/删除）
    memory_inspection：全面记忆巡检（遍历全部记忆，纠正/合并/精简/删除）
    skill_audit：技能自进化（巡检 skills_dir，创建/更新/删除技能）
    """
    common = f"""项目路径：{project_path}
session 文件：{session_path}

这是一个 JSONL 格式的对话记录，每行一条 JSON。

**重要：直接读取文件末尾（tail），定位最近一次完整对话。文件很大,不要从头读，只取末尾足够覆盖最近一次对话的行数即可。**"""

    if mode == "incremental_memory":
        return common + f"""

===== 印象快照 =====

**操作步骤：**
1. 直接 tail session.jsonl，取足够覆盖最近一次完整对话的行数（文件大时不要全读）
2. 用户最后说了什么？
3. 用了哪些工具（Read/Write/Edit/Bash/Grep/WebSearch 等），传了什么参数？
4. 工具返回了什么结果？
5. Claude 最终回复了什么？

如果最近一次对话信息不够判断，再往前多读几行追溯。

**根据最新对话判断是否有新增记忆：**
- 用户偏好类（语言风格、沟通习惯、技术栈偏好）→ mcp__SuperCC__MemoryAddUser / MemoryUpdateUser / MemoryDeleteUser
- 项目记忆类（文件路径、代码规范、bug、架构决策）→ mcp__SuperCC__MemoryAddProj / MemoryUpdateProj / MemoryDeleteProj

如有新增，直接调用 MCP 工具执行，完成后输出简短报告。

**输出模板：**
- 如果没有任何变更：直接输出「## 记忆增量报告：无变更」，不做其他说明
- 如果有变更：只列出有变动的项目，格式如下
```
## 记忆增量报告
- 新增用户偏好：X 条
- 新增项目记忆：X 条
- 更新记忆：X 条
- 删除记忆：X 条
```"""

    if mode == "memory_inspection":
        return common + f"""

===== 记忆精炼 =====

先对记忆库做全面巡检：
1. 调用 `mcp__SuperCC__MemoryListUser` 获取所有用户偏好
2. 对 project_path 调用 `mcp__SuperCC__MemoryListProj` 获取所有项目记忆

**全面巡检任务：**
- **纠正放错位置的记忆**：用户偏好中出现项目特有信息（文件路径、代码规范、bug、架构决策、git）或项目记忆中出现用户个人偏好（语言风格、沟通习惯），说明放错位置了 → 先在正确位置 MemoryAdd，再 MemoryDelete 旧的
- **合并重复**：内容高度相似的记忆，合并为一条最完整的
- **精简冗长**：啰嗦的记忆内容去掉重复表述，保留关键信息
- **删除过时**：已无价值或严重过时的记忆，用 MemoryDelete 删除

**输出模板：**
- 如果没有任何变更：直接输出「## 记忆巡检报告：无变更」
- 如果有变更：只列出有变动的项目，用具体描述（如「删除 2 条用户记忆」），不用分类标签
```
## 记忆巡检报告
- 新增记忆：X 条
- 纠正位置：X 条（如有，放错位置的应描述为「移入用户偏好」或「移入项目记忆」）
- 合并：X 条
- 精简：X 条
- 删除：X 条
```"""

    # skill_audit
    return common + f"""

===== 技能巡检 =====

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
- 只创建真正有价值的 Skill，不要为"有"而创建

**输出模板：**
- 如果没有任何变更：直接输出「## 技能巡检报告：无变更」
- 如果有变更：只列出有变动的项目（如「新增 1 个技能」），不用列出 0 条的
```
## 技能巡检报告
- 新增技能：X 个
- 更新技能：X 个
- 删除技能：X 个
```"""


# ── 记忆快照工具 ─────────────────────────────────────────────────────────────

def _snapshot_memory(project_path: str, user_open_id: str, platform: str, bot_id: str = "", chat_id: str = "", _log: logging.Logger | None = None) -> dict:
    """返回当前记忆库快照：{"user": [...], "proj": [...]}"""
    _log = _log or logger
    from supercc.core.memory_manager import MemoryManager
    mm = MemoryManager()  # 使用默认 ~/.supercc/memories.db
    user_items = mm.get_preferences_by_user(user_open_id, platform=platform, bot_id=bot_id) if user_open_id else []
    proj_items = mm.get_project_memories(project_path, platform=platform, chat_id=chat_id) or []
    _log.debug(f"snapshot — user_open_id={user_open_id!r}, platform={platform}, bot_id={bot_id!r}, chat_id={chat_id!r}, proj_path={project_path}, user_count={len(user_items)}, proj_count={len(proj_items)}")
    return {
        "user": [{"id": m.id, "title": m.title, "content": m.content, "keywords": m.keywords} for m in (user_items or [])],
        "proj": [{"id": m.id, "title": m.title, "content": m.content, "keywords": m.keywords} for m in (proj_items or [])],
    }


def _diff_memory(before: dict, after: dict) -> str:
    """对比前后记忆快照，返回变更描述（含更新检测）。"""
    # ── 逐条建 dict ──────────────────────────────────────────────
    def _index(items):
        return {m.get("id", ""): m for m in items}

    bu, au = _index(before["user"]), _index(after["user"])
    bp, ap = _index(before["proj"]), _index(after["proj"])

    added_user = set(au) - set(bu)
    del_user = set(bu) - set(au)
    added_proj = set(ap) - set(bp)
    del_proj = set(bp) - set(ap)

    # ── 检测更新 ──────────────────────────────────────────────────
    upd_user = []
    for iid in set(bu) & set(au):
        if bu[iid] != au[iid]:
            upd_user.append(iid)

    upd_proj = []
    for iid in set(bp) & set(ap):
        if bp[iid] != ap[iid]:
            upd_proj.append(iid)

    if not (added_user or del_user or added_proj or del_proj or upd_user or upd_proj):
        return "无变更"

    parts = []
    if added_user: parts.append(f"新增 {len(added_user)} 条用户记忆")
    if del_user: parts.append(f"删除 {len(del_user)} 条用户记忆")
    if upd_user: parts.append(f"更新 {len(upd_user)} 条用户记忆")
    if added_proj: parts.append(f"新增 {len(added_proj)} 条项目记忆")
    if del_proj: parts.append(f"删除 {len(del_proj)} 条项目记忆")
    if upd_proj: parts.append(f"更新 {len(upd_proj)} 条项目记忆")
    return "；".join(parts)


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


# ── Stream callbacks（仅记录日志，不推送）─────────────────────────────────────

async def _cb_mode1(
    msg: Any, push_fn: Callable, key: SessionKey, message_id: str, logger: logging.Logger, is_verbose: bool
) -> None:
    """印象快照：只记录日志，不推送任何消息。"""
    if msg.tool_name:
        tool_input = getattr(msg, 'tool_input', None)
        if msg.tool_name.startswith("mcp__SuperCC__Memory"):
            logger.info("[evolve] tool: %s — %s", msg.tool_name, tool_input)
        else:
            logger.debug("[evolve] tool: %s — %s", msg.tool_name, tool_input)


async def _cb_mode2(
    msg: Any, push_fn: Callable, key: SessionKey, message_id: str, logger: logging.Logger, is_verbose: bool
) -> None:
    """记忆精炼：纯 no-op，不推送任何消息。"""
    pass


async def _cb_mode3(
    msg: Any, push_fn: Callable, key: SessionKey, message_id: str, logger: logging.Logger, is_verbose: bool
) -> None:
    """技能巡检：只记录日志，不推送任何消息。"""
    if msg.content:
        logger.info("[evolve] text: %s", msg.content[:500])
    elif msg.tool_name:
        tool_input = getattr(msg, 'tool_input', None)
        if msg.tool_name.startswith("mcp__SuperCC__Memory"):
            logger.info("[evolve] tool: %s — %s", msg.tool_name, tool_input)
        else:
            logger.debug("[evolve] tool: %s — %s", msg.tool_name, tool_input)


# ── 权限配置工厂 ─────────────────────────────────────────────────────────────

# ── 主逻辑 ───────────────────────────────────────────────────────────────────

async def run_evolve(
    worker: Any,
    pool: Any,
    key: SessionKey,
    sdk_session_id: str,
    message_id: str,
    evo_context: dict | None,
    data_dir: str,
    push_fn: Callable[[Any], Any] | None,
    is_verbose_enabled_fn: Callable[[str, str, str], bool],
    config: Any,
    _logger: logging.Logger | None = None,
) -> None:
    """自进化主逻辑。

    - 每次对话后：执行印象快照（增量记忆）
    - 每 5 次对话后：顺序执行印象快照 → 技能巡检 → 记忆精炼（三者接续同一 session）

    evolve 使用独立的 SDK session（与主对话完全隔离），支持 resume 续接。
    由 executor.execute() 在主响应发送后异步调用，不阻塞主响应返回。
    """
    evo_logger = _logger or logger

    if not push_fn or not sdk_session_id:
        return

    if worker is None or worker.integration_evolve is None:
        return

    evo_resume = worker._sdk_session_id_evolve or None
    evo_logger.info(f"[evolve] start — sdk_session_id={sdk_session_id}, resume={evo_resume}")

    # ── 决定执行哪个模式 ─────────────────────────────────────────────
    evo_count = worker._evo_conversation_count
    do_full = evo_count >= 5
    evo_logger.debug(f"[evolve] count={evo_count}, full={'yes' if do_full else 'no'}")

    # ── 找 session 文件 ────────────────────────────────────────────────
    session_path = find_session_path(sdk_session_id)
    if not session_path:
        evo_logger.warning(f"[evolve] session file not found for {sdk_session_id}, skipping")
        return

    # ── MCP 工具上下文（使用 executor snapshot 的值，避免时序问题）──────────
    from supercc.core.message_context import set_current_context
    ctx = evo_context or {}
    set_current_context(
        user_open_id=ctx.get("user_open_id", ""),
        chat_id=ctx.get("chat_id", key.chat_id),
        platform=ctx.get("platform", key.platform),
        bot_id=ctx.get("bot_id", key.bot_id),
    )
    evo_logger.debug(f"[evolve] context set from snapshot: {ctx}")

    skills_dir = str(Path(data_dir) / "skills")
    evo_logger.debug(f"[evolve] data_dir={data_dir}, skills_dir={skills_dir}")
    is_evo_verbose = is_verbose_enabled_fn(key.platform, key.chat_id, "evolve")

    from supercc.core.evolve.skill_nudge import _get_skill_git_state, _get_skill_commit_message

    async def _skill_notify_wrapper(chat_id: str, text: str) -> None:
        if not push_fn:
            return
        from supercc.core.protocol import Event, MessageType, OutboundMessage
        notify_msg = OutboundMessage(
            event=Event.NOTIFICATION,
            session_key=key,
            message_id=message_id,
            content=text,
            message_type=MessageType.TEXT,
        )
        await push_fn(notify_msg)

    try:
        worker.integration_evolve.approved_directory = skills_dir
        # ── Mode 1（每次必跑）：bypassPermissions + MCP-only + Read ─────────
        # 不管 do_full 如何，Mode 1 都要跑
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
            # Mode 1/2：用 disallowed_tools 禁用所有内置工具，只允许 Read + MCP 工具
            opts.disallowed_tools = [
                "Write", "Edit", "Bash", "Grep",
                "NotRecommend", "WebSearch", "WebFetch",
                "NotebookEdit", "TaskStart", "TaskComplete",
                "Agent", "Skill",
            ]

        # ── 全流程入口快照 ─────────────────────────────────────────────
        before_skill_state = await _get_skill_git_state(Path(skills_dir))
        before_memory_state = _snapshot_memory(key.project_path, ctx.get("user_open_id", ""), ctx.get("platform", "feishu"), bot_id=ctx.get("bot_id", ""), chat_id=ctx.get("chat_id", ""), _log=evo_logger)

        prompt_m1 = build_evolve_prompt(session_path, key.project_path, skills_dir, mode="incremental_memory")
        cb1 = lambda msg: _cb_mode1(msg, push_fn, key, message_id, evo_logger, is_evo_verbose)
        task = asyncio.create_task(worker.integration_evolve.query(prompt=prompt_m1, on_stream=cb1))
        worker._current_task_evolve = task
        try:
            res_m1, evo_sid, _ = await task
        finally:
            worker._current_task_evolve = None
        if evo_sid:
            worker._sdk_session_id_evolve = evo_sid
        if res_m1:
            evo_logger.info(f"[evolve] 印象快照 done, session={evo_sid}, result={res_m1[:500]}")
        else:
            evo_logger.info(f"[evolve] 印象快照 done, session={evo_sid}")

        # ── Mode 3（巡检时才跑）：auto + can_use_tool 路径限制 ─────────────
        if do_full:
            # can_use_tool 回调：auto 模式下 CLI 调用回调获取 allow/deny 决策，
            # 返回 PermissionResultDeny 拦截项目路径写入，不弹 prompt。
            def _extract_paths(command: str, cwd: str) -> tuple[list[Path], bool]:
                """
                从 Bash 命令中提取所有路径并 resolve。
                返回 (paths, has_cd_to_proj) — paths 是 resolve 后的路径列表，
                has_cd_to_proj 表示是否有 cd 到 project_path 的子命令。
                """
                # 检查 cd/pushd 是否指向 project_path
                proj_re = re.escape(str(Path(key.project_path).resolve()))
                cd_match = re.search(
                    rf'(?:^|\s)(?:cd|pushd)\s+["\']?({proj_re})["\']?',
                    command
                )
                has_cd_proj = bool(cd_match)

                # 1. 相对路径检测：任何不以 / 开头的 token（排除 flag 和已知子命令）
                tokens = command.strip().split()
                rel_paths = []
                for tok in tokens:
                    if tok.startswith('-'):
                        continue  # flag
                    if tok in ('git', 'rm', 'ls', 'tail', 'head'):
                        continue  # 命令本身
                    if tok.startswith('/'):
                        continue  # 绝对路径
                    # 过滤：纯数字或不包含路径特征的 token 不是路径
                    if '/' not in tok and '.' not in tok and not tok.startswith('~'):
                        continue
                    rel_paths.append(tok)

                # 2. 绝对路径提取
                abs_tokens = re.findall(r'(?:^|\s)(/[\w./\U00000800-\U0010FFFF-]+)', command)

                paths = []
                for token in rel_paths:
                    p = Path(token)
                    if not p.is_absolute():
                        p = (Path(cwd) / p).resolve()
                    paths.append(p)
                for token in abs_tokens:
                    paths.append(Path(token))
                return paths, has_cd_proj

            async def skill_audit_can_use_tool(
                tool_name: str, tool_input: dict[str, Any], ctx: Any
            ) -> Any:
                from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
                skills_path = Path(skills_dir).resolve()
                proj_path = Path(key.project_path).resolve()

                # Bash: 解析命令中的路径；其他工具: 取 path/file_path
                if tool_name == "Bash":
                    command = tool_input.get("command") or ""
                    # 1. 命令白名单：只允许 git, rm, ls, tail, head
                    cmd_parts = command.strip().split()
                    cmd = cmd_parts[0] if cmd_parts else ""
                    if cmd not in ("git", "rm", "ls", "tail", "head"):
                        evo_logger.warning(f"[evolve] can_use_tool denied: command '{cmd}' not in whitelist")
                        return PermissionResultDeny(message="安全限制：仅支持 git/rm/ls/tail/head 五条指令")
                    # 2. 路径白名单：只允许绝对路径，相对路径全部拒绝
                    paths, has_cd_proj = _extract_paths(command, str(skills_path))
                    # cd 到 project_path 是安全红线
                    if has_cd_proj:
                        evo_logger.warning(
                            f"[evolve] can_use_tool denied: cd to project path in command"
                        )
                        return PermissionResultDeny(
                            message=f"安全限制：禁止 cd 到项目目录 {proj_path}"
                        )
                else:
                    path_str = tool_input.get("path") or tool_input.get("file_path") or ""
                    # 非 Bash 工具也必须用绝对路径，相对路径拒绝
                    if path_str and not Path(path_str).is_absolute():
                        evo_logger.warning(f"[evolve] can_use_tool denied: relative path in {tool_name}: {path_str}")
                        return PermissionResultDeny(message="安全限制：仅支持绝对路径")
                    paths = [Path(path_str).resolve()] if path_str else []
                    has_cd_proj = False

                for file_path in paths:
                    try:
                        is_proj = str(file_path).startswith(str(proj_path))
                        is_skill = str(file_path).startswith(str(skills_path))
                        # project_path 上只有 Read/Grep 允许，其他全部拒绝
                        if is_proj:
                            if tool_name in ("Read", "Grep"):
                                return PermissionResultAllow(behavior="allow")
                            evo_logger.warning(
                                f"[evolve] can_use_tool denied {tool_name} on project path: {file_path}"
                            )
                            return PermissionResultDeny(
                                message=f"安全限制：禁止对项目目录使用 {tool_name}"
                            )
                        # 技能目录内 — 全部放行
                        if is_skill:
                            return PermissionResultAllow(behavior="allow")
                    except Exception:
                        pass
                return PermissionResultAllow(behavior="allow")

            worker.integration_evolve._init_options(
                channel=key.platform,
                continue_conversation=False,
                session_id=None,
                resume=evo_sid,
            )
            if worker.integration_evolve._options is not None:
                opts = worker.integration_evolve._options
                # auto 模式：CLI 会发权限查询给 can_use_tool 回调，
                # 回调返回 allow/deny 控制路径访问，不弹 prompt。
                opts.permission_mode = "auto"
                opts.sandbox = {"enabled": True, "excludedCommands": ["git"]}
                opts.can_use_tool = skill_audit_can_use_tool
                opts.disallowed_tools = []  # 清掉 Mode 1 的禁用列表，让 can_use_tool 做权限控制

            prompt_m3 = build_evolve_prompt(session_path, key.project_path, skills_dir, mode="skill_audit")
            cb3 = lambda msg: _cb_mode3(msg, push_fn, key, message_id, evo_logger, is_evo_verbose)
            task = asyncio.create_task(worker.integration_evolve.query(prompt=prompt_m3, on_stream=cb3))
            worker._current_task_evolve = task
            try:
                res_m3, evo_sid, _ = await task
            finally:
                worker._current_task_evolve = None
            if evo_sid:
                worker._sdk_session_id_evolve = evo_sid
            if res_m3:
                evo_logger.info(f"[evolve] 技能巡检 done, session={evo_sid}, result={res_m3[:500]}")
            else:
                evo_logger.info(f"[evolve] 技能巡检 done, session={evo_sid}")

            # ── Mode 2（巡检时才跑）：bypassPermissions + MCP-only + Read ───
            # Mode 2 接续 Mode 3 的上下文，承接 Mode 3 发现的待记事项
            worker.integration_evolve._init_options(
                channel=key.platform,
                continue_conversation=False,
                session_id=None,
                resume=evo_sid,
            )
            if worker.integration_evolve._options is not None:
                opts = worker.integration_evolve._options
                opts.permission_mode = "bypassPermissions"
                opts.sandbox = {"enabled": True, "excludedCommands": ["git"]}
                opts.disallowed_tools = [
                    "Write", "Edit", "Bash", "Grep",
                    "NotRecommend", "WebSearch", "WebFetch",
                    "NotebookEdit", "TaskStart", "TaskComplete",
                    "Agent",
                ]

            prompt_m2 = build_evolve_prompt(session_path, key.project_path, skills_dir, mode="memory_inspection")
            task = asyncio.create_task(worker.integration_evolve.query(prompt=prompt_m2, on_stream=None))
            worker._current_task_evolve = task
            try:
                res_m2, evo_sid, _ = await task
            finally:
                worker._current_task_evolve = None
            if evo_sid:
                worker._sdk_session_id_evolve = evo_sid
            if res_m2:
                evo_logger.info(f"[evolve] 记忆精炼 done, session={evo_sid}, result={res_m2[:500]}")
            else:
                evo_logger.info(f"[evolve] 记忆精炼 done, session={evo_sid}")

            worker._evo_conversation_count = 0

        # ── 全流程完成：统一检测变更，verbose 时推送汇总通知 ───────────
        if is_evo_verbose and push_fn:
            after_memory = _snapshot_memory(key.project_path, ctx.get("user_open_id", ""), ctx.get("platform", "feishu"), bot_id=ctx.get("bot_id", ""), chat_id=ctx.get("chat_id", ""))
            mem_diff = _diff_memory(before_memory_state, after_memory)
            after_skill = await _get_skill_git_state(Path(skills_dir))
            skill_changes = []
            for skill_name, sha in after_skill.items():
                before_sha = before_skill_state.get(skill_name)
                if before_sha is None and sha is not None:
                    msg = _get_skill_commit_message(skills_dir, skill_name, sha)
                    skill_changes.append(f"🆕 新建 {skill_name}（{msg}）")
                elif sha != before_sha and sha is not None:
                    msg = _get_skill_commit_message(skills_dir, skill_name, sha)
                    skill_changes.append(f"🔄 更新 {skill_name}（{msg}）")
            for skill_name, before_sha in before_skill_state.items():
                if skill_name not in after_skill and before_sha is not None:
                    skill_changes.append(f"🗑️ 删除 {skill_name}")

            has_mem = mem_diff != "无变更"
            has_skill = bool(skill_changes)

            if has_mem or has_skill:
                lines = ["🔄自进化提醒："]
                if has_mem:
                    lines.append(f"- 🧠记忆更新：{mem_diff}")
                if has_skill:
                    lines.append(f"- 🧰技能更新：{'，'.join(skill_changes)}")
                text = "\n".join(lines)
                evo_logger.info(f"[evolve] 推送自进化提醒: {text}")
                await _skill_notify_wrapper(key.chat_id, text)
            else:
                evo_logger.info("[evolve] 无任何技能/记忆变更")

        evo_logger.info(f"[evolve] done for {key} (full={do_full})")
    except Exception as e:
        import traceback
        # 清空 session ID，下次 evolve 自动开新 session（避免 session 损坏后一直重试失败）
        worker._sdk_session_id_evolve = ""
        worker._current_task_evolve = None
        evo_logger.warning(f"[evolve] failed: {e}\n{traceback.format_exc()}")


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

"""Skill self-evolution — triggers skill review after N tool calls.

This module tracks tool call count per session and triggers a background
review when the threshold is reached. When skill changes are detected,
notifies the triggering chat immediately (no polling).

Key components:
- SkillNudge: tracks tool call count per session
- SKILL_NUDGE_PROMPT: review prompt template
- _get_skill_git_state / _get_skill_commit_message / _detect_skill_changes: git-based change detection
- trigger_skill_review: calls _detect_skill_changes with notify=True after review
- _ensure_symlinks: syncs skills to ~/.claude/skills/ (called by cron_scheduler)
- _ensure_skills_git_repo / README_CONTENT: git repo setup for skills directory
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

README_CONTENT = """\
# Skills 目录

此目录用于存放**用户自己的** Claude Code 自定义 Skill。

**重要**：此目录是一个本地 Git 仓库（没有 remote，不支持 push）。
Skill 由 CC 自动维护，Git 仅用于本地历史管理和回退。
如需安装来自 GitHub 或其他来源的第三方 Skill，请安装到 `~/.claude/skills/` 目录，勿放在此处。

每个 Skill 是一个独立目录，包含 `SKILL.md` 文件，格式如下：

```markdown
---
name: skill-name
description: 技能描述
author: your-name
version: 1.0.0
---

# 技能名称

技能正文内容...
```
"""


def _ensure_skills_git_repo(skills_dir: Path) -> None:
    """Ensure skills_dir exists and is a git repo, creating README if needed."""
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)

    # Check if git is available
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        git_available = result.returncode == 0
    except Exception:
        git_available = False

    if not git_available:
        logger.debug("[skill_nudge] git not available, skipping git repo initialization")
        return

    # Check if skills_dir itself is a git repo (not a parent repo)
    git_path = skills_dir / ".git"
    is_git = git_path.exists()

    if not is_git:
        subprocess.run(["git", "init"], cwd=str(skills_dir), capture_output=True)
        is_git = True
        logger.info(f"[skill_nudge] initialized git repo at {skills_dir}")

    # Always ensure README exists
    readme_path = skills_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(README_CONTENT, encoding="utf-8")
        if is_git:
            subprocess.run(
                ["git", "add", "README.md"],
                cwd=str(skills_dir),
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "初始化 Skills 目录"],
                cwd=str(skills_dir),
                capture_output=True,
            )
            logger.info(f"[skill_nudge] created README at {skills_dir}")


def _ensure_symlinks(skills_dir: Path, symlink_dir: Path | None = None) -> None:
    """Ensure all skills in skills_dir have a corresponding symlink in symlink_dir.

    Symlinks are created in ~/.claude/skills/ by default.
    Idempotent: existing correct symlinks are left as-is.
    """
    symlink_dir = symlink_dir or (Path.home() / ".claude" / "skills")
    if not skills_dir.exists():
        return
    symlink_dir.mkdir(parents=True, exist_ok=True)
    for skill_path in skills_dir.iterdir():
        if not skill_path.is_dir():
            continue
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            continue
        symlink_path = symlink_dir / skill_path.name
        if symlink_path.exists() or symlink_path.is_symlink():
            if symlink_path.resolve() == skill_path.resolve():
                logger.debug(f"[skill_nudge] symlink {symlink_path.name} already points to {skill_path}, skipping")
                continue
            try:
                symlink_path.unlink()
            except PermissionError:
                if symlink_path.is_dir():
                    symlink_path.rmdir()
                else:
                    raise
        symlink_path.symlink_to(skill_path)
        logger.info(f"[skill_nudge] symlinked {skill_path.name}")


@dataclass
class SkillNudgeConfig:
    enabled: bool = True
    interval: int = 10  # trigger after N tool calls
    current_user: str = ""  # used as author match for auto-evolve


@dataclass
class SkillNudge:
    """Tracks tool call count and triggers review when threshold is hit."""
    config: SkillNudgeConfig
    _count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: bool = False  # True while a review is in flight

    def reset(self) -> None:
        with self._lock:
            self._count = 0
            self._pending = False

    def increment(self) -> bool:
        """Increment tool call count. Returns True if review should be triggered."""
        if not self.config.enabled:
            return False
        with self._lock:
            if self._pending:
                return False
            self._count += 1
            if self._count >= self.config.interval:
                self._pending = True
                return True
            return False

    def mark_review_done(self) -> None:
        """Call when review is complete to reset counter."""
        with self._lock:
            self._count = 0
            self._pending = False


def make_nudge(config: SkillNudgeConfig) -> SkillNudge:
    return SkillNudge(config=config)


# Review prompt shown to Claude Code when nudge fires
SKILL_NUDGE_PROMPT = """\
根据当前对话历史，判断是否有值得创建或更新的 Skill。

**记忆 vs 技能的边界**：
- **记忆**：记录事实、偏好、项目决策、bug 修复、用户信息。→ 用 MCP memory 工具
- **技能**：记录可复用的工作流、多步骤操作流程、跨 session 的通用模式。→ 创建 Skill

适合存为 Skill 的场景：
- 发现了可推广的非平凡工作流程
- 用户要求记住某个多步骤操作流程
- 经过多次迭代验证的可靠方法

**创建前必须先搜索记忆确认**：调用 mcp__SuperCC__MemorySearchUser / MemorySearchProj
检查是否已有相同内容。已有则不再创建 Skill。

**SKILL.md 元数据格式**（YAML frontmatter）：
```yaml
---
name: skill-name
description: 简短描述
version: 1.0
usage_count: 0        # 使用次数，每被成功执行一次 +1
status: active        # active / archived
created_at: 2026-01-01  # 创建日期
updated_at: 2026-01-01  # 最近修改日期
---
```

**每个新 Skill 创建时，必须在正文末尾追加以下使用追踪指令**（固定文案，一字不差）：
---
> **使用追踪**：每次你读取此 SKILL.md 并成功按指示执行后，请将 frontmatter 的 `usage_count` +1、更新 `updated_at` 为当天日期。如果是更新 Skill 内容，只更新 `updated_at`，不改 `usage_count`。

操作步骤：
1. 先搜索记忆确认不重复，再查看 {SKILLS_DIR}/ 下已有 Skill
2. 把完整内容直接写入 {SKILLS_DIR}/<skill-name>/SKILL.md，包含完整 frontmatter + 使用追踪指令
3. {SKILLS_DIR}/ 是一个本地 Git 仓库（没有 remote，不支持 push）。
   写入 SKILL.md 后，进入该目录执行：
   ```
   cd {SKILLS_DIR} && git add <skill-name>/ && git commit -m "<中文 commit message>"
   ```
   commit message 必须用中文，清晰说明本次改动内容。
   **不要执行 git push**——此仓库只有本地历史，没有远程仓库。

**淘汰规则**（基于使用情况和当前项目现状自动处理）：
- 一星期没有被使用的 skill → `status` 设为 `archived`
- 一个月没有被使用 + 判断真的无用/过时（对当前项目而言）→ **直接删除**
- 每次技能自进化扫描时，结合当前项目现状和记忆，检查所有 skill 的上述指标并执行对应操作

注意：
- 只创建真正有价值的 Skill，不要为了"有"而创建
- 如果有相关 Skill 已存在，优先更新它而不是创建新的
- **记忆中已有相关描述时，不要再创建冗余的 Skill**（先搜索记忆确认）
- **已存在的 Skill 缺少 usage tracking 元数据时，自动补全**（frontmatter 加 usage_count/status/created_at/updated_at，末尾加使用追踪指令）
- 更新 Skill 时只改正文 instructions，不要动 frontmatter 的 name/description
- 新建和更新不需要确认，发现就直接做
"""


def _get_skill_git_state(skills_dir: Path) -> dict[str, str | None]:
    """Get current git state: {skill_name: latest_commit_sha or None}."""
    state: dict[str, str | None] = {}
    if not skills_dir.exists():
        return state
    # Quick check if git is available at all
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return state
    except Exception:
        return state
    for skill_path in skills_dir.iterdir():
        if not skill_path.is_dir():
            continue
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H", "--", skill_path.name],
                cwd=str(skills_dir),
                capture_output=True, text=True,
            )
            sha = result.stdout.strip() or None
            state[skill_path.name] = sha
        except Exception:
            state[skill_path.name] = None
    return state


def _get_skill_commit_message(skills_dir: Path, skill_name: str, sha: str) -> str:
    """Get the commit message for a given SHA."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", sha],
            cwd=str(skills_dir),
            capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


async def _detect_skill_changes(
    before_state: dict[str, str | None],
    skills_dir: Path,
    chat_id: str | None = None,
    send_to_feishu: Callable[[str, str], Awaitable[None]] | None = None,
    notify: bool = True,
) -> None:
    """Compare before/after git state, detect changes (new/updated/deleted), notify user."""
    after_state = _get_skill_git_state(skills_dir)
    logger.info(f"[skill_nudge] checking skills dir: {skills_dir.resolve()}")
    logger.debug(f"[skill_nudge] before_state={before_state}")
    logger.debug(f"[skill_nudge] after_state={after_state}")

    changed = []
    for skill_name, sha in after_state.items():
        before_sha = before_state.get(skill_name)
        if before_sha is None and sha is not None:
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🆕 新建", "commit": msg})
        elif sha != before_sha and sha is not None:
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🔄 更新", "commit": msg})

    for skill_name, before_sha in before_state.items():
        if skill_name not in after_state and before_sha is not None:
            changed.append({"name": skill_name, "action": "🗑️ 删除", "commit": ""})

    if not changed:
        logger.info("[skill_nudge] no git changes detected, skipping notification")
        return

    parts = []
    for c in changed:
        commit_info = f"（{c['commit']}）" if c["commit"] else ""
        parts.append(f"{c['action']} **{c['name']}**{commit_info}")

    msg = "🧰 Skill 自进化：" + "、".join(parts)

    if notify and chat_id and send_to_feishu:
        try:
            await send_to_feishu(chat_id, msg)
            logger.info(f"[skill_nudge] notification sent: {msg}")
        except Exception as e:
            logger.warning(f"[skill_nudge] failed to send to Feishu: chat_id={chat_id!r}, error={e}")
    else:
        logger.info(f"[skill_nudge] changes detected but notify={notify}, chat_id={chat_id!r}, send_to_feishu={send_to_feishu}")


async def trigger_skill_review(
    make_claude_query: Callable[..., Awaitable[tuple]],
    nudge: SkillNudge,
    chat_id: str | None = None,
    send_to_feishu: Callable[[str, str], Awaitable[None]] | None = None,
    skills_dir: Path | None = None,
) -> None:
    """Trigger a background skill review by calling Claude Code.

    Args:
        make_claude_query: a callable that runs a Claude query and returns
            (response_text, session_id, cost)
        nudge: the SkillNudge instance to manage counter and pending state
        chat_id: Feishu chat_id to deliver results to (optional)
        send_to_feishu: async callable(chat_id, text) to send a Feishu message (optional)
        skills_dir: path to skills directory (defaults to ~/.supercc/skills/)
    """
    if not nudge or not nudge.config.enabled:
        return

    logger.info("[skill_nudge] triggering skill review")

    from supercc.config import get_config
    skills_dir = skills_dir or (Path(get_config().data_dir) / "skills")

    # Snapshot before state
    before_state = _get_skill_git_state(skills_dir)

    try:
        prompt = SKILL_NUDGE_PROMPT.format(
            SKILLS_DIR=str(skills_dir),
        )
        response, _, _ = await make_claude_query(prompt)
        logger.info(f"[trigger_skill_review] done: {response[:200] if response else '(empty)'}")

        # Detect changes via git state comparison, then notify the triggering chat immediately
        await _detect_skill_changes(
            before_state=before_state,
            skills_dir=skills_dir,
            chat_id=chat_id,
            send_to_feishu=send_to_feishu,
            notify=True,
        )

    except Exception as e:
        logger.warning(f"[skill_nudge] review failed: {e}")
    finally:
        if nudge:
            nudge.mark_review_done()


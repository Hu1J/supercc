"""Hermes-style skill nudge — triggers skill review after N tool calls.

This module tracks tool call count per session and triggers a background
review when the threshold is reached, asking Claude Code to consider
creating or updating a skill based on recent conversation patterns.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

README_CONTENT = """\
# Skills 目录

此目录用于存放**用户自己的** Claude Code 自定义 Skill。

**重要**：此目录下的 Skill 由 CC 自动维护，通过 Git 管理历史和回退。
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
                continue
            symlink_path.unlink()
        symlink_path.symlink_to(skill_path)
        logger.info(f"[skill_nudge] symlinked {skill_path.name}")


# Review prompt shown to Claude Code when nudge fires
# Claude writes skills directly to {SKILLS_DIR}/ and manages git commits there
SKILL_NUDGE_PROMPT = """\
根据当前对话历史，判断是否有值得创建或更新的 Skill。

适合存为 Skill 的场景：
- 解决了非平凡问题，且解决方法可推广
- 发现了一种新的工作流程或技巧
- 克服了错误并找到了正确方法
- 用户要求记住某个流程

操作步骤：
1. 先查看 {SKILLS_DIR}/ 目录下已有的 Skill
2. 把完整内容直接写入 {SKILLS_DIR}/<skill-name>/SKILL.md
3. 格式：YAML frontmatter (name/description/author/version) + Markdown body
4. {SKILLS_DIR}/ 本身是一个 Git 仓库。写入 SKILL.md 后，进入该目录执行：
   ```
   cd {SKILLS_DIR} && git add <skill-name>/ && git commit -m "<中文 commit message>"
   ```
   commit message 必须用中文，清晰说明本次改动内容

注意：
- 只创建真正有价值的 Skill，不要为了"有"而创建
- 如果有相关 Skill 已存在，优先更新它而不是创建新的
- 更新 Skill 时只改正文 instructions，不要动 frontmatter 的 name/description
"""


def _parse_skill_meta(content: str) -> tuple[str, str, str]:
    """Returns (name, description, author) from SKILL.md frontmatter."""
    name, description, author = "", "", ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if line.startswith("name:"):
                    name = line.split("name:", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split("description:", 1)[1].strip()
                elif line.startswith("author:"):
                    author = line.split("author:", 1)[1].strip()
    return name, description, author


def _get_skill_git_state(skills_dir: Path) -> dict[str, str | None]:
    """Get current git state: {skill_name: latest_commit_sha or None}."""
    state: dict[str, str | None] = {}
    if not skills_dir.exists():
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

    changed = []
    for skill_name, sha in after_state.items():
        before_sha = before_state.get(skill_name)
        if before_sha is None and sha is not None:
            # New skill
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🆕 新建", "commit": msg})
        elif sha != before_sha and sha is not None:
            # Updated skill
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🔄 更新", "commit": msg})

    # Detect deleted skills
    for skill_name, before_sha in before_state.items():
        if skill_name not in after_state and before_sha is not None:
            changed.append({"name": skill_name, "action": "🗑️ 删除", "commit": ""})

    if not changed:
        return

    # Build notification with commit messages
    parts = []
    for c in changed:
        commit_info = f"（{c['commit']}）" if c['commit'] else ""
        parts.append(f"{c['action']} **{c['name']}**{commit_info}")

    msg = "🧰 Skill 自进化：" + "、".join(parts)

    if notify and chat_id and send_to_feishu:
        try:
            await send_to_feishu(chat_id, msg)
        except Exception as e:
            logger.warning(f"[skill_nudge] failed to send to Feishu: {e}")


async def poll_skill_changes_and_notify(
    data_dir: str,
    skills_dir: Path,
    send_to_feishu: Callable[[str, str], Awaitable[None]] | None = None,
    get_chat_id: Callable[[str], str | None] | None = None,
) -> None:
    """Poll skills directory for changes and notify user.

    Stores last known state in data_dir/.skill_poll_state.json.
    Detects new, updated, and deleted skills since last poll.
    Sends notification to the current active chat_id.
    """
    state_file = Path(data_dir) / ".skill_poll_state.json"

    # Load last state
    last_state: dict[str, str | None] = {}
    if state_file.exists():
        try:
            last_state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            last_state = {}

    current_state = _get_skill_git_state(skills_dir)

    # First run: ensure symlinks for all existing skills, then save state
    if not last_state:
        symlink_dir = Path.home() / ".claude" / "skills"
        _ensure_symlinks(skills_dir, symlink_dir)
        state_file.write_text(json.dumps(current_state, ensure_ascii=False), encoding="utf-8")
        return

    # Detect changes
    changed = []
    for skill_name, sha in current_state.items():
        before_sha = last_state.get(skill_name)
        if before_sha is None and sha is not None:
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🆕 新建", "commit": msg})
        elif sha != before_sha and sha is not None:
            msg = _get_skill_commit_message(skills_dir, skill_name, sha)
            changed.append({"name": skill_name, "action": "🔄 更新", "commit": msg})

    for skill_name, before_sha in last_state.items():
        if skill_name not in current_state and before_sha is not None:
            changed.append({"name": skill_name, "action": "🗑️ 删除", "commit": ""})

    # Save current state
    state_file.write_text(json.dumps(current_state, ensure_ascii=False), encoding="utf-8")

    # Always ensure symlinks on every tick (idempotent — safe to call repeatedly)
    symlink_dir = Path.home() / ".claude" / "skills"
    _ensure_symlinks(skills_dir, symlink_dir)

    if not changed:
        return

    # Send notification
    parts = []
    for c in changed:
        commit_info = f"（{c['commit']}）" if c['commit'] else ""
        parts.append(f"{c['action']} **{c['name']}**{commit_info}")

    msg = "🧰 Skill 自进化：" + "、".join(parts)

    chat_id = get_chat_id(data_dir) if get_chat_id else None
    if chat_id and send_to_feishu:
        try:
            await send_to_feishu(chat_id, msg)
        except Exception as e:
            logger.warning(f"[poll_skill_changes] failed to send to Feishu: {e}")


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
        skills_dir: path to skills directory (defaults to ~/.cc-feishu-bridge/skills/)
    """
    if not nudge or not nudge.config.enabled:
        return

    logger.info("[skill_nudge] triggering skill review")

    skills_dir = skills_dir or (Path.home() / ".cc-feishu-bridge" / "skills")

    # Snapshot before state
    before_state = _get_skill_git_state(skills_dir)

    try:
        prompt = SKILL_NUDGE_PROMPT.format(
            SKILLS_DIR=str(skills_dir),
        )
        response, _, _ = await make_claude_query(prompt)
        logger.info(f"[trigger_skill_review] done: {response[:200] if response else '(empty)'}")

        # Detect changes via git state comparison (don't notify — poll_skill_changes_and_notify handles that)
        await _detect_skill_changes(
            before_state=before_state,
            skills_dir=skills_dir,
            chat_id=chat_id,
            send_to_feishu=send_to_feishu,
            notify=False,
        )

    except Exception as e:
        logger.warning(f"[skill_nudge] review failed: {e}")
    finally:
        if nudge:
            nudge.mark_review_done()


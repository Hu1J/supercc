"""会话状态 — /status"""
import asyncio
import os
import re
from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.protocol import SessionKey
from supercc.core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH


class StatusHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "status"

    @property
    def help(self) -> str:
        return "/status — 显示会话状态"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc import __version__
        from supercc.core.models.model_config import get_model_env
        from supercc.core.models.model_providers import PROVIDERS

        session_key: SessionKey = context.get("session_key")
        project_path = session_key.project_path if session_key else ""

        # 获取活跃 session
        sm = SessionManager(db_path=DEFAULT_SESSIONS_DB_PATH)
        session = None
        if session_key:
            session = sm.get_active_session(
                bot_id=session_key.bot_id,
                project_path=session_key.project_path,
                platform=session_key.platform,
                chat_id=session_key.chat_id,
            )

        # 当前分支
        branch = "(无分支)"
        try:
            import subprocess
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                text=True, timeout=5, cwd=project_path,
            ).strip()
        except Exception:
            pass

        # 当前模型
        try:
            env = get_model_env()
            pid = env.provider_id
            mid = env.ANTHROPIC_MODEL
            provider = PROVIDERS.get(pid)
            model_provider = provider.id if provider else (pid or "未设置")
            model_id = mid or "未设置"
        except Exception:
            model_provider = "未知"
            model_id = "未知"

        # SDK session ID
        sdk_sid = session.sdk_session_id if session and session.sdk_session_id else "(未建立)"

        # 版本更新检查
        title = f"🐲 **SuperCC v{__version__}**"
        try:
            from supercc.core.commands.restart_impl import check_version
            current_ver, latest_ver = await asyncio.to_thread(check_version)
            def _ver_gt(current: str, latest: str) -> bool:
                def nums(v):
                    return [int(x) for x in re.findall(r"\d+", v)]
                return nums(latest) > nums(current)
            if _ver_gt(current_ver, latest_ver):
                title = f"🐲 **SuperCC v{__version__} — 🌟可更新 v{latest_ver}🌟**"
        except Exception:
            pass

        # 统计技能数量
        def _count_skills(skills_dir: str) -> int:
            try:
                from pathlib import Path
                p = Path(skills_dir)
                if not p.exists():
                    return 0
                return sum(1 for item in p.iterdir() if item.is_dir() and (item / "SKILL.md").exists())
            except Exception:
                return 0

        project_skills = _count_skills(os.path.join(project_path, ".supercc", "skills"))
        global_skills = _count_skills(os.path.expanduser("~/.claude/skills"))

        table = (
            f"{title}\n\n"
            f"| 项目 | 值 |\n"
            f"|------|----|\n"
            f"| 进程ID | `{os.getpid()}` |\n"
            f"| 会话ID | `{sdk_sid}` |\n"
            f"| 消息数 | {session.message_count if session else 0} |\n"
            f"| 累计费用 | `${session.total_cost if session else 0:.4f}` |\n"
            f"| 供应商 | {model_provider} |\n"
            f"| 模型ID | `{model_id}` |\n"
            f"| Git分支 | `{branch}` |\n"
            f"| 工作目录 | `{project_path}` |\n"
            f"| 项目技能数 | {project_skills} |\n"
            f"| 全局技能数 | {global_skills} |"
        )
        return CommandResult(content=table)

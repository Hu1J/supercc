"""会话状态 — /status"""
import os
from core.commands.base import CommandHandler, CommandResult, CommandCard
from core.protocol import SessionKey


class StatusHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "status"

    @property
    def help(self) -> str:
        return "/status — 显示会话状态"

    async def execute(self, args: str, context: dict) -> CommandResult:
        from supercc import __version__
        from supercc.claude.model_config import get_model_env
        from supercc.claude.model_providers import PROVIDERS

        session_key: SessionKey = context.get("session_key")
        project_path = session_key.project_path if session_key else ""

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
            model_info = f"{provider.id if provider else pid} / `{mid or '未设置'}`"
        except Exception:
            model_info = "未知"

        card_data = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"🐲 **SuperCC v{__version__}**\n\n"
                            f"| 项目 | 值 |\n"
                            f"|------|----|\n"
                            f"| 进程ID | `{os.getpid()}` |\n"
                            f"| 模型 | {model_info} |\n"
                            f"| Git分支 | `{branch or '(无分支)'}` |\n"
                            f"| 工作目录 | `{project_path}` |"
                        ),
                    },
                ]
            },
        }
        return CommandResult(content="", card=CommandCard(type="interactive", data=card_data))
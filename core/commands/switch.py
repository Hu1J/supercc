"""切换项目 — /switch"""
import os
from core.commands.base import CommandHandler, CommandResult


class SwitchHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "switch"

    @property
    def help(self) -> str:
        return "/switch <目标项目路径> — 切换到另一个项目"

    async def execute(self, args: str, context: dict) -> CommandResult:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 1 or not parts[0]:
            return CommandResult(
                content="用法: /switch <目标项目路径>\n例: /switch /Users/x/my-project"
            )

        raw_path = parts[0].strip()
        if raw_path.startswith("/") or raw_path.startswith("~"):
            target = os.path.expanduser(raw_path)
        else:
            target = os.path.abspath(raw_path)

        return CommandResult(
            content=f"正在切换到 `{target}`...",
            event="switch",
            extra={"target_path": target},
        )

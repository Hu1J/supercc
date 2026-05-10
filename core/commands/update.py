"""更新 — /update"""
from core.commands.base import CommandHandler, CommandResult


class UpdateHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "update"

    @property
    def help(self) -> str:
        return "/update — 检查并更新到最新版本"

    async def execute(self, args: str, context: dict) -> CommandResult:
        return CommandResult(content="正在检查更新...", event="update")

"""重启 — /restart"""
from core.commands.base import CommandHandler, CommandResult


class RestartHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "restart"

    @property
    def help(self) -> str:
        return "/restart — 重启 SuperCC"

    async def execute(self, args: str, context: dict) -> CommandResult:
        return CommandResult(content="正在重启...", event="restart")

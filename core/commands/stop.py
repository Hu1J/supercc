"""停止查询 — /stop"""
from core.commands.base import CommandHandler, CommandResult


class StopHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "stop"

    @property
    def help(self) -> str:
        return "/stop — 打断当前查询"

    async def execute(self, args: str, context: dict) -> CommandResult:
        return CommandResult(content="🛑 已打断当前任务。")
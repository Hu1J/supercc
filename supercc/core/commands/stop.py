"""停止查询 — /stop"""
from supercc.core.commands.base import CommandHandler, CommandResult
from supercc.core.protocol import SessionKey


class StopHandler(CommandHandler):
    @property
    def name(self) -> str:
        return "stop"

    @property
    def help(self) -> str:
        return "/stop — 打断当前查询"

    async def execute(self, args: str, context: dict) -> CommandResult:
        worker_pool = context.get("worker_pool")
        session_key: SessionKey = context.get("session_key")

        if worker_pool and session_key:
            await worker_pool.stop(session_key)

        return CommandResult(content="🛑 已打断当前任务。")

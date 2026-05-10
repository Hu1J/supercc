"""命令路由器 — 将命令名分发给对应 Handler。"""
from core.commands.base import CommandHandler, CommandResult
from core.commands.help import HelpHandler
from core.commands.status import StatusHandler
from core.commands.new_session import NewSessionHandler
from core.commands.stop import StopHandler
from core.commands.git import GitHandler
from core.commands.model import ModelHandler
from core.commands.codex import CodexHandler
from core.commands.verbose import VerboseHandler
from core.commands.memory import MemoryHandler
from core.commands.skill import SkillHandler


class CommandRouter:
    def __init__(self):
        self._handlers: dict[str, CommandHandler] = {}
        self._register_all()

    def _register_all(self):
        for h in [
            HelpHandler(),
            StatusHandler(),
            NewSessionHandler(),
            StopHandler(),
            GitHandler(),
            ModelHandler(),
            CodexHandler(),
            VerboseHandler(),
            MemoryHandler(),
            SkillHandler(),
        ]:
            self._handlers[h.name] = h

    async def dispatch(self, command_name: str, args: str, context: dict) -> CommandResult:
        handler = self._handlers.get(command_name.lower())
        if handler is None:
            return CommandResult(content=f"❓ 未知命令: /{command_name}")
        try:
            return await handler.execute(args, context)
        except Exception as e:
            return CommandResult(content=f"⚠️ 命令执行失败: {e}")
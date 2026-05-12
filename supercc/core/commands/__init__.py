"""Core slash command handlers."""
from supercc.core.commands.base import CommandHandler, CommandResult, CommandCard
from supercc.core.commands.codex import CodexHandler
from supercc.core.commands.git import GitHandler
from supercc.core.commands.help import HelpHandler
from supercc.core.commands.memory import MemoryHandler
from supercc.core.commands.model import ModelHandler
from supercc.core.commands.new_session import NewSessionHandler
from supercc.core.commands.skill import SkillHandler
from supercc.core.commands.status import StatusHandler
from supercc.core.commands.stop import StopHandler
from supercc.core.commands.verbose import VerboseHandler
from supercc.core.commands.router import CommandRouter

__all__ = [
    "CommandHandler",
    "CommandResult",
    "CommandCard",
    "CodexHandler",
    "GitHandler",
    "HelpHandler",
    "MemoryHandler",
    "ModelHandler",
    "NewSessionHandler",
    "SkillHandler",
    "StatusHandler",
    "StopHandler",
    "VerboseHandler",
    "CommandRouter",
]
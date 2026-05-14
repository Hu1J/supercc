"""斜杠命令基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CommandResult:
    """命令执行结果。Core 返回 content，Plugin 负责渲染决策。"""
    content: str = ""
    extra: dict = field(default_factory=dict)
    event: str = "response"


class CommandHandler(ABC):
    """斜杠命令处理器基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """命令名称，如 'help'、'status'。"""
        ...

    @property
    def help(self) -> str:
        """简短帮助文本。"""
        return ""

    @abstractmethod
    async def execute(self, args: str, context: dict) -> CommandResult:
        """
        执行命令。

        Args:
            args: 命令参数（不含命令名本身）
            context: 执行上下文，含：
                - session_key: core.protocol.SessionKey
                - user_open_id: str
                - chat_id: str
                - platform: str
                - config: supercc.config.Config
                - data_dir: str

        Returns:
            CommandResult: 含 content（Plugin 用 should_use_card() 判断如何渲染）
        """
        ...
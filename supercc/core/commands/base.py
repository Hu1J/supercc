"""斜杠命令基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CommandCard:
    """CardKit 格式的卡片数据。"""
    type: str = "markdown"
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


@dataclass
class CommandResult:
    """命令执行结果。plugin 据此决定渲染文本还是卡片。"""
    content: str = ""
    card: Optional[CommandCard] = None
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
            CommandResult: 含 content 或 card
        """
        ...
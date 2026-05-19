"""asyncio contextvars for current message context — allows tool functions to access user/chat info without explicit param passing."""
from contextvars import ContextVar

# 当前消息上下文，供工具函数使用
# 在 SessionWorker._process_message 中设置，工具函数通过 get_current_user_open_id() / get_current_chat_id() 读取
_current_user_open_id: ContextVar[str | None] = ContextVar("current_user_open_id", default=None)
_current_chat_id: ContextVar[str | None] = ContextVar("current_chat_id", default=None)
_current_platform: ContextVar[str] = ContextVar("current_platform", default="feishu")
_current_bot_id: ContextVar[str | None] = ContextVar("current_bot_id", default=None)


def set_current_context(user_open_id: str, chat_id: str, platform: str = "feishu", bot_id: str = ""):
    """在消息处理入口设置当前上下文。"""
    _current_user_open_id.set(user_open_id)
    _current_chat_id.set(chat_id)
    _current_platform.set(platform)
    _current_bot_id.set(bot_id)


def get_current_user_open_id() -> str | None:
    return _current_user_open_id.get()


def get_current_chat_id() -> str | None:
    return _current_chat_id.get()


def get_current_platform() -> str:
    return _current_platform.get()


def get_current_bot_id() -> str | None:
    return _current_bot_id.get()

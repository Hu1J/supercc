"""WeCom (企业微信) adapter for SuperCC."""
from supercc.adapter.wecom.client import WeComClient, WeComIncomingMessage
from supercc.adapter.wecom.ws_client import WeComWSClient
from supercc.adapter.wecom.message_handler import MessageHandler

__all__ = [
    "WeComClient",
    "WeComIncomingMessage",
    "WeComWSClient",
    "MessageHandler",
]

"""QQ channel plugin for SuperCC.

QQ Official Bot API v2:
- Inbound: WebSocket Gateway (wss://api.sgroup.qq.com/)
- Outbound: REST API (api.sgroup.qq.com)

Supports:
- C2C_MESSAGE_CREATE: Private chat
- GROUP_AT_MESSAGE_CREATE: Group @mention messages
"""
from supercc.channels.qq.ws_client import QQWSClient
from supercc.channels.qq.client import QQClient, IncomingQQMessage

__all__ = ["QQWSClient", "QQClient", "IncomingQQMessage"]
"""微信个人（iLink）channel 插件。"""
from supercc.channels.wechat.lp_client import WeChatLongPollingClient
from supercc.channels.wechat.client import WeChatClient
from supercc.channels.wechat.core_client import WeChatCoreWSClient

__all__ = ["WeChatLongPollingClient", "WeChatClient", "WeChatCoreWSClient"]
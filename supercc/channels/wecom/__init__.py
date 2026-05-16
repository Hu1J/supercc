"""企业微信（WeCom）适配器 — Thin Client 模式连接核心服务。"""
from supercc.channels.wecom.core_client import WeComCoreWSClient
from supercc.channels.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable

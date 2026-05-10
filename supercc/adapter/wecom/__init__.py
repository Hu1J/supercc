"""企业微信（WeCom）适配器 — Thin Client 模式连接核心服务。"""
from supercc.adapter.wecom.core_client import WeComCoreWSClient
from supercc.adapter.wecom.core_protocol import incoming_to_inbound, outbound_to_renderable

"""WhatsApp 适配器 — Thin Client 模式连接核心服务（通过 Node.js bridge）。"""
from supercc.channels.whatsapp.core_client import WhatsAppCoreWSClient
from supercc.channels.whatsapp.format.reply_formatter import format_whatsapp_markdown
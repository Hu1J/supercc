"""消息转换：WhatsApp message ↔ 核心 InboundMessage / OutboundMessage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from supercc.core.protocol import (
    SessionKey, InboundMessage, OutboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from supercc.core.protocol import OutboundMessage as OutboundMessageType


@dataclass
class WhatsAppIncomingMessage:
    """WhatsApp 插件的 incoming message 结构。"""
    msgid: str           # 消息 ID
    chat_id: str         # 会话 ID
    sender_id: str       # 发送者 ID
    sender_name: str      # 发送者名称
    content: str          # 消息内容
    message_type: str = "text"  # text / image / file / audio
    timestamp: str = ""   # 时间戳
    reply_to: str = ""    # 回复的消息 ID


def incoming_to_inbound(
    incoming: WhatsAppIncomingMessage | dict[str, Any],
    bot_id: str,
    project_path: str,
    system_prompt: str = "",
    group_members: list | None = None,
    group_context: str = "",
) -> InboundMessage:
    """
    将 WhatsApp IncomingMessage 转换为核心 InboundMessage。

    bot_id: 从配置读取的 WhatsApp bot ID
    project_path: 从配置读取的项目路径
    system_prompt: 插件注入的群聊指令（群名/成员规则），core 追加到 system prompt
    group_members: 群成员列表（raw dict 格式），core 用于 mention 自动补全检测
    group_context: 群聊对话上下文（历史/引用），非指令时前置到 user prompt
    """
    # 支持 dict 或 dataclass
    if isinstance(incoming, dict):
        msg_id = incoming.get("msgid", "")
        chat_id = incoming.get("chat_id", "")
        sender_id = incoming.get("sender_id", "")
        sender_name = incoming.get("sender_name", "")
        content = incoming.get("content", "")
        message_type = incoming.get("message_type", "text")
    else:
        msg_id = incoming.msgid
        chat_id = incoming.chat_id
        sender_id = incoming.sender_id
        sender_name = incoming.sender_name
        content = incoming.content
        message_type = incoming.message_type

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="whatsapp",
        chat_id=chat_id,
    )

    # 消息角色
    role = MessageRole.USER

    # 消息类型
    msg_type_map = {
        "text": MessageType.TEXT,
        "image": MessageType.IMAGE,
        "file": MessageType.FILE,
        "audio": MessageType.FILE,
    }
    msg_type = msg_type_map.get(message_type, MessageType.TEXT)

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=msg_id,
        role=role,
        content=content,
        message_type=msg_type,
        media_path=None,
        user_open_id=sender_id or None,
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": incoming if isinstance(incoming, dict) else {},
            "is_group_chat": False,  # WhatsApp 暂不支持群聊
            "mention_bot": False,
            "mention_ids": [],
            "sender_name": sender_name,
            "chat_type": "p2p",
        },
        system_prompt=system_prompt,
        group_context=group_context,
    )


@dataclass
class WhatsAppRenderable:
    """可渲染的 WhatsApp 消息单元。"""
    chat_id: str
    message_id: str  # 用于回复/更新
    content: str     # Markdown 或原始文本


def outbound_to_renderable(
    outbound: "OutboundMessageType",
    reply_to_message_id: str | None = None,
) -> WhatsAppRenderable:
    """
    将核心 OutboundMessage 转换为 WhatsApp 可渲染格式。

    reply_to_message_id: 用于回复同一消息（引用）
    """
    from supercc.channels.whatsapp.format.reply_formatter import should_use_card

    content = outbound.content

    return WhatsAppRenderable(
        chat_id=outbound.session_key.chat_id,
        message_id=reply_to_message_id or "",
        content=content,
    )
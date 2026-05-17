"""消息转换：Telegram 消息 ↔ 核心 InboundMessage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from supercc.core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from supercc.core.protocol import OutboundMessage


def incoming_to_inbound(
    msg: dict,
    bot_id: str,
    project_path: str,
    system_prompt: str = "",
    group_members: list | None = None,
    group_context: str = "",
) -> InboundMessage:
    """
    将 Telegram 消息字典转换为核心 InboundMessage。

    bot_id: 从配置读取的 Telegram bot token（或 bot username）
    project_path: 从配置读取的项目路径
    system_prompt: 插件注入的群聊指令（群名/成员/@规则），core 追加到 system prompt
    group_members: 群成员列表，core 用于 @mention 自动补全检测
    group_context: 群聊对话上下文（历史/引用），非指令时前置到 user prompt

    Telegram 入站消息格式:
    {
        "msgid": "123",
        "chat_id": "123456789",
        "user_id": "987654321",
        "username": "john_doe",
        "content": "Hello bot",
        "date": "2024-01-01T12:00:00",
        "chat_type": "private" | "group",
    }
    """
    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="telegram",
        chat_id=msg.get("chat_id", ""),
    )

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=msg.get("msgid", ""),
        role=MessageRole.USER,
        content=msg.get("content", ""),
        message_type=MessageType.TEXT,
        media_path=None,
        user_open_id=msg.get("user_id", ""),
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": str(msg),
            "is_group_chat": msg.get("chat_type") == "group",
            "mention_bot": False,  # Telegram uses @username mention in text
            "mention_ids": [],
            "group_name": "",
            "chat_type": msg.get("chat_type", "private"),
            "username": msg.get("username", ""),
            "group_members": group_members or [],
        },
        system_prompt=system_prompt,
        group_context=group_context,
    )


@dataclass
class TelegramRenderable:
    """可渲染的 Telegram 消息单元。"""
    chat_id: str
    message_id: str  # 用于回复/更新
    content: str     # MarkdownV2 格式
    use_card: bool = False  # Telegram 无原生卡片


def outbound_to_renderable(
    outbound: "OutboundMessage",
    reply_to_message_id: str | None = None,
) -> TelegramRenderable:
    """将核心 OutboundMessage 转换为 Telegram 可渲染格式。"""
    return TelegramRenderable(
        chat_id=outbound.session_key.chat_id,
        message_id=reply_to_message_id or "",
        content=outbound.content,
        use_card=False,  # Telegram 无原生卡片
    )
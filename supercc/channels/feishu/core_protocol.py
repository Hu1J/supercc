"""消息转换：Feishu IncomingMessage ↔ 核心 InboundMessage / OutboundMessage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from supercc.channels.feishu.client import IncomingMessage
from supercc.core.protocol import (
    SessionKey, InboundMessage, OutboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from supercc.core.protocol import OutboundMessage as OutboundMessageType


def incoming_to_inbound(
    incoming: IncomingMessage,
    bot_id: str,
    project_path: str,
    system_prompt: str = "",
    group_members: list | None = None,
    group_context: str = "",
) -> InboundMessage:
    """
    将 Feishu IncomingMessage 转换为核心 InboundMessage。

    bot_id: 从配置读取的飞书机器人 open_id
    project_path: 从配置读取的项目路径
    system_prompt: 插件注入的群聊指令（群名/成员/@规则），core 追加到 system prompt
    group_members: 群成员列表（raw dict 格式），core 用于 @mention 自动补全检测
    group_context: 群聊对话上下文（历史/引用），非指令时前置到 user prompt
    """
    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="feishu",
        chat_id=incoming.chat_id,
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
    msg_type = msg_type_map.get(incoming.message_type, MessageType.TEXT)

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=incoming.message_id,
        role=role,
        content=incoming.content,
        message_type=msg_type,
        media_path=None,  # 媒体路径由 media.py 下载后在 content 中嵌入
        user_open_id=incoming.user_open_id or None,
        thread_id=incoming.thread_id or None,
        timestamp=_cst_now(),
        extra={
            "raw": incoming.raw_content,
            "is_group_chat": incoming.is_group_chat,
            "mention_bot": incoming.mention_bot,
            "mention_ids": incoming.mention_ids,
            "group_name": incoming.group_name,
            "chat_type": incoming.chat_type,
            "group_members": group_members or [],
        },
        system_prompt=system_prompt,
        group_context=group_context,
    )


@dataclass
class FeishuRenderable:
    """可渲染的飞书消息单元。"""
    chat_id: str
    message_id: str  # 用于回复/更新
    content: str     # Markdown 或原始文本
    use_card: bool = False  # 是否使用 Interactive Card


def outbound_to_renderable(
    outbound: "OutboundMessageType",
    reply_to_message_id: str | None = None,
) -> FeishuRenderable:
    """
    将核心 OutboundMessage 转换为飞书可渲染格式。

    reply_to_message_id: 用于回复同一消息（引用）
    """
    from supercc.channels.feishu.format.reply_formatter import should_use_card

    content = outbound.content
    use_card = should_use_card(content)

    return FeishuRenderable(
        chat_id=outbound.session_key.chat_id,
        message_id=reply_to_message_id or "",
        content=content,
        use_card=use_card,
    )

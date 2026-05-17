"""消息转换：DingTalk 入站消息 ↔ 核心 InboundMessage."""

from __future__ import annotations

from supercc.core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)


def incoming_to_inbound(
    incoming: dict,
    bot_id: str,
    project_path: str,
    system_prompt: str = "",
    group_members: list | None = None,
    group_context: str = "",
) -> InboundMessage:
    """
    将 DingTalk 入站消息字典转换为核心 InboundMessage。

    Args:
        incoming: DingTalkWSClient._parse_message() 返回的字典
        bot_id: 从配置读取的钉钉机器人 app_key
        project_path: 从配置读取的项目路径
        system_prompt: 插件注入的群聊指令（群名/成员/@规则）
        group_members: 群成员列表（raw dict 格式）
        group_context: 群聊对话上下文（历史/引用）
    """
    chat_id = incoming.get("chat_id", "")
    user_id = incoming.get("user_staff_id", "") or incoming.get("user_id", "")
    content = incoming.get("content", "")
    is_group = incoming.get("is_group", False)
    conversation_type = incoming.get("conversation_type", "1")

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="dingtalk",
        chat_id=chat_id,
    )

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=incoming.get("msgid", ""),
        role=MessageRole.USER,
        content=content,
        message_type=MessageType.TEXT,
        media_path=None,
        user_open_id=user_id or None,
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": incoming,
            "is_group_chat": is_group,
            "mention_bot": False,
            "mention_ids": [],
            "group_name": "",
            "chat_type": "group" if is_group else "dm",
            "group_members": group_members or [],
            "conversation_type": conversation_type,
        },
        system_prompt=system_prompt,
        group_context=group_context,
    )

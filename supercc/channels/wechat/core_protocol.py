"""消息转换：WeChat 微信消息 → 核心 InboundMessage."""

from __future__ import annotations

from typing import Any

from supercc.core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)


def incoming_to_inbound(
    msg: dict[str, Any],
    bot_id: str,
    project_path: str,
    account_id: str,
    system_prompt: str = "",
    group_context: str = "",
) -> InboundMessage:
    """
    将微信消息 dict 转换为核心 InboundMessage。

    从 Long Polling 回调原始 msg dict 提取字段，构建 InboundMessage
    供 server._handle_wechat_message 处理。
    """
    sender_id = str(msg.get("from_user_id") or "").strip()
    message_id = str(msg.get("message_id") or "").strip()

    item_list = msg.get("item_list") or []
    text = _extract_text(item_list)

    # 检测群聊
    chat_type, chat_id = _guess_chat_type(msg, account_id)
    is_group_chat = chat_type == "group"

    # 消息类型
    msg_type_map = {
        "text": MessageType.TEXT,
        "image": MessageType.IMAGE,
        "file": MessageType.FILE,
        "audio": MessageType.FILE,
    }
    # 从 item_list 推断 message_type
    message_type_str = "text"
    if item_list:
        first_type = item_list[0].get("type")
        if first_type == 2:
            message_type_str = "image"
        elif first_type in {3, 4, 5}:
            message_type_str = "file"

    msg_type = msg_type_map.get(message_type_str, MessageType.TEXT)

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="wechat",
        chat_id=chat_id if is_group_chat else sender_id,
    )

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=message_id,
        role=MessageRole.USER,
        content=text,
        message_type=msg_type,
        media_path=None,
        user_open_id=sender_id or None,
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": msg,
            "is_group_chat": is_group_chat,
            "mention_bot": False,
            "mention_ids": [],
            "group_name": "",
            "chat_type": "group" if is_group_chat else "p2p",
        },
        system_prompt=system_prompt,
        group_context=group_context,
    )


# ── 内部工具（从 core_client.py 迁移）───────────────────────────────────────

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    """从 item_list 中提取文本内容。"""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    """判断是群聊还是私聊。"""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")

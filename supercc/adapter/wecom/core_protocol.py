"""消息转换：WeCom 消息 ↔ 核心 InboundMessage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from core.protocol import OutboundMessage


def _check_mention_bot(msg: dict) -> bool:
    """检测消息是否 @ 了机器人。"""
    mentioned_list = msg.get("mentioned_list", [])
    bot_id = msg.get("aibotid", "")
    return bot_id in mentioned_list


def incoming_to_inbound(
    msg: dict,
    bot_id: str,
    project_path: str,
) -> InboundMessage:
    """
    将 WeCom 消息字典转换为核心 InboundMessage。

    WeCom 入站消息格式:
    {
        "msgid": "xxx",
        "aibotid": "bot_xxx",
        "chattype": "single" | "group",
        "chatid": "oc_xxx",
        "from": { "userid": "ou_xxx" },
        "msgtype": "text" | "image" | "file",
        "text": { "content": "..." },
        "image": { "media_id": "..." },
        ...
    }
    """
    msg_type = msg.get("msgtype", "text")
    content = ""
    if msg_type == "text":
        content = msg.get("text", {}).get("content", "")
    elif msg_type == "image":
        content = "[图片]"
    elif msg_type == "file":
        content = "[文件]"
    elif msg_type == "voice":
        content = "[语音]"

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="wecom",
        chat_id=msg.get("chatid", ""),
    )

    msg_type_map = {
        "text": MessageType.TEXT,
        "image": MessageType.IMAGE,
        "file": MessageType.FILE,
        "voice": MessageType.FILE,
    }

    return InboundMessage(
        event="message",
        session_key=key,
        message_id=msg.get("msgid", ""),
        role=MessageRole.USER,
        content=content,
        message_type=msg_type_map.get(msg_type, MessageType.TEXT),
        media_path=None,
        user_open_id=msg.get("from", {}).get("userid", ""),
        thread_id=None,
        timestamp=_cst_now(),
        extra={
            "raw": str(msg),
            "is_group_chat": msg.get("chattype") == "group",
            "mention_bot": _check_mention_bot(msg),  # ← 改为函数调用
            "mention_ids": msg.get("mentioned_list", []),  # ← 从 [] 改为 msg.get()
            "group_name": "",
            "chat_type": msg.get("chattype", "single"),
        },
    )


@dataclass
class WeComRenderable:
    """可渲染的企业微信消息单元。"""
    chat_id: str
    content: str  # Markdown
    use_markdown: bool = True


def outbound_to_renderable(outbound: "OutboundMessage") -> WeComRenderable:
    """将核心 OutboundMessage 转换为企业微信可渲染格式。"""
    return WeComRenderable(
        chat_id=outbound.session_key.chat_id,
        content=outbound.content,
        use_markdown=True,  # WeCom 原生支持 Markdown
    )

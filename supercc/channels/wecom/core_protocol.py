"""消息转换：WeCom 消息 ↔ 核心 InboundMessage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from supercc.core.protocol import (
    SessionKey, InboundMessage,
    MessageRole, MessageType, _cst_now,
)

if TYPE_CHECKING:
    from supercc.core.protocol import OutboundMessage


def _check_mention_bot(msg: dict) -> bool:
    """检测消息是否 @ 了机器人。"""
    mentioned_list = msg.get("mentioned_list", [])
    bot_id = msg.get("aibotid", "")
    return bot_id in mentioned_list


def incoming_to_inbound(
    msg: dict,
    bot_id: str,
    project_path: str,
    system_prompt: str = "",
    group_members: list | None = None,
    group_context: str = "",
) -> InboundMessage:
    """
    将 WeCom 消息字典转换为核心 InboundMessage。

    bot_id: 从配置读取的企业微信机器人 agent_id
    project_path: 从配置读取的项目路径
    system_prompt: 插件注入的群聊指令（群名/成员/@规则），core 追加到 system prompt
    group_members: 群成员列表，core 用于 @mention 自动补全检测
    group_context: 群聊对话上下文（历史/引用），非指令时前置到 user prompt

    WeCom 入站消息格式:
    {
        "msgid": "xxx",
        "aibotid": "bot_xxx",
        "chattype": "single" | "group",
        "chatid": "oc_xxx",
        "from": { "userid": "ou_xxx" },
        "msgtype": "text" | "image" | "file" | "voice" | "mixed",
        "text": { "content": "..." },
        "image": { "url": "https://...", "aeskey": "..." },
        "file": { "url": "https://...", "aeskey": "...", "name": "xxx.pdf", "size": 123 },
        ...
    }
    """
    msg_type = msg.get("msgtype", "text")
    content = ""
    extra_fields = {}

    # _resolved_content：由 send_message 在解析媒体后设置，优先使用
    resolved_content = msg.get("_resolved_content", "")

    if msg_type == "text":
        content = msg.get("text", {}).get("content", "")
    elif msg_type == "image":
        content = resolved_content if resolved_content else "[图片]"
        img = msg.get("image", {})
        extra_fields["image_url"] = img.get("url", "")
        extra_fields["image_aeskey"] = img.get("aeskey", "")
    elif msg_type == "file":
        file_info = msg.get("file", {})
        content = resolved_content if resolved_content else f"[文件: {file_info.get('name', '未知文件')}]"
        extra_fields["file_url"] = file_info.get("url", "")
        extra_fields["file_aeskey"] = file_info.get("aeskey", "")
        extra_fields["file_name"] = file_info.get("name", "")
    elif msg_type == "voice":
        content = "[语音]"
    elif msg_type == "mixed":
        content = "[混合消息]"

    key = SessionKey(
        bot_id=bot_id,
        project_path=project_path,
        platform="wecom",
        chat_id=msg.get("chatid", "") or msg.get("from", {}).get("userid", ""),
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
            "mention_bot": _check_mention_bot(msg),
            "mention_ids": msg.get("mentioned_list", []),
            "group_name": "",
            "chat_type": msg.get("chattype", "single"),
            "group_members": group_members or [],
            # 企业微信特有字段
            "room_id": msg.get("roomid", ""),
            "sender_id": msg.get("from", {}).get("userid", ""),
            # 媒体下载字段（image/file 消息有 url + aeskey）
            **extra_fields,
        },
        system_prompt=system_prompt,
        group_context=group_context,
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

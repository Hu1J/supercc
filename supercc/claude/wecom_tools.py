"""WeCom 文件发送 MCP 工具 — 暴露 WeComSendFile 给 Claude Code 使用。

注意：企业微信的 upload_media 使用 3-step WebSocket 协议，
需要在 WeCom 插件进程中有活跃的 WS 连接才能调用。
WeComSendFile 通过在 MCP 工具进程内创建独立的 SDK WSClient 实例来实现
（connect 用于触发认证，但不接收消息），适用于偶发的文件上传场景。
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from claude_agent_sdk import tool


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


WECOM_FILE_GUIDE = """
【企业微信文件】用户要求发送文件/图片/截图时，调用 mcp__SuperCC__WeComSendFile(file_paths: list[str])。
"""


def _resolve_path(file_path: str) -> str:
    """将相对路径尝试解析为绝对路径。"""
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
    if os.path.exists(file_path):
        return os.path.abspath(file_path)

    candidates = []
    try:
        from supercc.config import get_config
        cfg = get_config()
        approved = cfg.claude.approved_directory
        if approved:
            candidates.append(os.path.join(approved, file_path))
    except Exception:
        pass

    candidates.append(os.path.join(os.getcwd(), file_path))

    for candidate in candidates:
        if os.path.exists(candidate):
            return os.path.abspath(candidate)

    return file_path


def _get_wecom_credentials() -> tuple[str, str]:
    """从 config 获取 WeCom bot_id 和 secret。"""
    from supercc.config import get_config
    cfg = get_config()
    wecom_cfg = cfg.channels.wecom
    bot_id = wecom_cfg.bot_id or wecom_cfg.agent_id
    secret = wecom_cfg.secret or wecom_cfg.corp_secret
    return bot_id, secret


def _get_chat_id() -> Optional[str]:
    """从 contextvar 获取当前 chat_id。"""
    from supercc.claude.message_context import get_current_chat_id
    return get_current_chat_id()


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件，返回 msg_id 或抛出异常。"""
    from wecom_aibot_sdk import WSClient, WSClientOptions

    bot_id, secret = _get_wecom_credentials()
    options = WSClientOptions(bot_id=bot_id, secret=secret)
    sdk_client = WSClient(options)

    resolved_path = _resolve_path(file_path)
    ext = os.path.splitext(resolved_path)[1].lower()
    file_name = os.path.basename(resolved_path)

    try:
        media_result = await sdk_client.upload_media(resolved_path)
        media_id = media_result.media_id

        if ext in SUPPORTED_IMAGE_EXTS:
            msg_id = await sdk_client.send_image(chat_id, media_id)
        else:
            msg_id = await sdk_client.send_file(chat_id, media_id)

        return msg_id
    finally:
        # 不需要保持 WS 连接，上传完成后关闭
        try:
            await sdk_client.disconnect()
        except Exception:
            pass


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "WeComSendFile",
    "发送本地文件或图片到企业微信用户（通过当前活跃会话的 chat_id）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def wecom_send_file(args: dict) -> dict:
    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}
    if not isinstance(file_paths, list):
        return {"content": [{"type": "text", "text": "file_paths 必须是列表"}], "is_error": True}

    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃企业微信会话，请先在企业微信里发一条消息"}],
            "is_error": True,
        }

    # 验证文件
    errors = []
    for fp in file_paths:
        resolved = _resolve_path(fp)
        if not os.path.exists(resolved):
            errors.append(f"文件不存在: {fp}")
        elif os.path.getsize(resolved) > MAX_FILE_SIZE:
            errors.append(f"{os.path.basename(fp)} 超过 30MB 限制")
    if errors:
        return {"content": [{"type": "text", "text": "\n".join(errors)}], "is_error": True}

    # 并发发送
    async def send_one(fp: str) -> tuple[str, str | None]:
        try:
            await _send_single_file(fp, chat_id)
            return (fp, None)
        except Exception as e:
            return (fp, str(e))

    results = await asyncio.gather(*[send_one(fp) for fp in file_paths])

    ok = [fp for fp, err in results if err is None]
    fail = [(fp, err) for fp, err in results if err is not None]

    lines = []
    if ok:
        lines.append(f"✅ 已发送 {len(ok)} 个文件")
        for fp in ok:
            lines.append(f"  • {os.path.basename(fp)}")
    if fail:
        lines.append(f"❌ 失败 {len(fail)} 个")
        for fp, err in fail:
            lines.append(f"  • {os.path.basename(fp)}: {err}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}

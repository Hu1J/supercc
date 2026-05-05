"""WeCom 文件发送 MCP 工具 — 暴露 WeComSendFile 给 Claude Code 使用。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from claude_agent_sdk import tool
from supercc.config import SESSIONS_DB_PATH


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


WECOM_FILE_GUIDE = """
【企业微信文件】用户要求发送文件/图片/截图时，调用 mcp__SuperCC__WeComSendFile(file_paths: list[str])。
企业微信发送文件需要 URL，不支持直接上传本地文件。
"""


def _get_wecom_client() -> "WeComClient":
    """延迟初始化 WeComClient（读取 config.json）。"""
    from supercc.config import get_config
    from supercc.adapter.wecom.client import WeComClient
    cfg = get_config()
    return WeComClient(
        bot_id=cfg.channels.wecom.bot_id,
        bot_secret=cfg.channels.wecom.bot_secret,
        bot_name=cfg.channels.wecom.bot_name,
        data_dir=str(cfg.claude.approved_directory),
    )


def _get_chat_id() -> Optional[str]:
    """从 contextvar 获取当前 chat_id。"""
    from supercc.claude.message_context import get_current_chat_id
    return get_current_chat_id()


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件/URL，返回 msg_id 或抛出异常。"""
    from supercc.adapter.wecom.client import WeComClient

    wecom = _get_wecom_client()
    ext = os.path.splitext(file_path)[1].lower()
    file_name = os.path.basename(file_path)

    if ext in SUPPORTED_IMAGE_EXTS:
        msg_id = await wecom.send_image(chat_id, file_path)
    else:
        msg_id = await wecom.send_file(chat_id, file_path, file_name)

    return msg_id


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "WeComSendFile",
    "发送本地文件或图片到企业微信用户（通过当前活跃会话的 chat_id）。"
    "企业微信要求文件必须为可访问的 URL，不支持直接上传本地文件。"
    "如果用户提供了本地文件路径，请先提醒：企业微信发送文件需要 URL。",
    {"file_paths": list},
)
async def wecom_send_file(args: dict) -> dict:
    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}
    if not isinstance(file_paths, list):
        return {"content": [{"type": "text", "text": "file_paths 必须是列表"}], "is_error": True}

    # 获取 chat_id
    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃企业微信会话，请先在企业微信里发一条消息"}],
            "is_error": True,
        }

    # 验证所有文件
    # 对于 URL：不检查文件存在性，直接发送
    # 对于本地路径：检查存在性和大小，但最终仍需要是 URL
    errors = []
    local_files = []
    for fp in file_paths:
        if not fp.startswith(("http://", "https://", "ftp://")):
            # 本地文件路径
            if not os.path.exists(fp):
                errors.append(f"文件不存在: {fp}")
            elif os.path.getsize(fp) > MAX_FILE_SIZE:
                errors.append(f"{os.path.basename(fp)} 超过 30MB 限制")
            else:
                local_files.append(fp)
        else:
            # URL，无需验证
            pass

    if local_files:
        errors.append(
            f"企业微信不支持直接上传本地文件。以下文件需要先上传到可访问的 URL: "
            + ", ".join(os.path.basename(f) for f in local_files)
        )

    if errors:
        return {"content": [{"type": "text", "text": "\n".join(errors)}], "is_error": True}

    # 并发发送
    async def send_one(fp: str) -> tuple[str, str | None]:
        try:
            msg_id = await _send_single_file(fp, chat_id)
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

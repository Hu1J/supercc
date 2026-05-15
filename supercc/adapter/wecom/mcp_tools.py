"""WeCom MCP 工具 — 暴露企业微信特有工具给 core 调用。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from claude_agent_sdk import tool

# 全局 WeComClient，由 mcp_server.main() 初始化
_wecom_client: Optional["WeComClient"] = None
_current_chat_id: Optional[str] = None


def init(wecom_client: "WeComClient", chat_id: str | None = None):
    """初始化全局 WeComClient 和 chat_id。"""
    global _wecom_client, _current_chat_id
    _wecom_client = wecom_client
    _current_chat_id = chat_id


def set_chat_id(chat_id: str):
    """设置当前 chat_id（每次 MCP 调用前由 core 设置）。"""
    global _current_chat_id
    _current_chat_id = chat_id


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


def _resolve_path(file_path: str) -> str:
    """将相对路径解析为绝对路径。"""
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


@tool(
    "WeComSendFile",
    "发送本地文件或图片到企业微信（通过当前活跃会话的 chat_id）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def wecom_send_file(args: dict) -> dict:
    """发送本地文件到企业微信。"""
    if _wecom_client is None:
        return {"content": [{"type": "text", "text": "WeComClient not initialized"}], "is_error": True}

    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}

    chat_id = _current_chat_id
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃企业微信会话"}],
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
            resolved = _resolve_path(fp)
            ext = os.path.splitext(resolved)[1].lower()
            media_id = await _wecom_client.upload_media(resolved)
            if ext in SUPPORTED_IMAGE_EXTS:
                msg_id = await _wecom_client.send_image(chat_id, media_id)
            else:
                msg_id = await _wecom_client.send_file(chat_id, media_id)
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

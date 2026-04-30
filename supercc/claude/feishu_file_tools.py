"""Feishu 文件发送 MCP 工具 — 暴露 FeishuSendFile 给 Claude Code 使用。"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Optional

from claude_agent_sdk import tool
from supercc.config import SESSIONS_DB_PATH


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


FEISHU_FILE_GUIDE = """
【飞书文件发送】当用户要求发送文件/图片/截图/压缩包时，调用 mcp__SuperCC__FeishuSendFile(file_paths: list[str])，MCP 自动从当前会话获取 chat_id。
"""


def _resolve_path(file_path: str) -> str:
    """将相对路径尝试解析为绝对路径。

    尝试顺序：原始路径 → 当前工作目录 → SuperCC 配置的 approved_directory。
    只在相对路径且原路径不存在时才尝试解析。
    """
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
    if os.path.exists(file_path):
        return os.path.abspath(file_path)

    # 相对路径：尝试从 config 里的 approved_directory 解析
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

    # 找不到就返回原路径，让后面的 open() 报文件不存在
    return file_path


def _get_feishu_client() -> "FeishuClient":
    """延迟初始化 FeishuClient（读取 config.yaml）。"""
    from supercc.config import get_config
    from supercc.adapter.feishu.client import FeishuClient
    cfg = get_config()
    return FeishuClient(
        app_id=cfg.channels.feishu.app_id,
        app_secret=cfg.channels.feishu.app_secret,
    )


def _get_chat_id() -> Optional[str]:
    """从当前活跃会话获取 chat_id（按 project_path 过滤）。"""
    from supercc.config import get_config
    project_path = get_config().claude.approved_directory
    with sqlite3.connect(SESSIONS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT chat_id FROM sessions WHERE chat_id IS NOT NULL AND project_path = ? ORDER BY last_used DESC LIMIT 1",
            (project_path,),
        ).fetchone()
    return row["chat_id"] if row else None


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件，返回 msg_id 或抛出异常。"""
    from supercc.adapter.feishu.media import guess_file_type
    from supercc.adapter.feishu.client import FeishuClient

    feishu = _get_feishu_client()
    resolved_path = _resolve_path(file_path)
    ext = os.path.splitext(resolved_path)[1].lower()
    file_name = os.path.basename(resolved_path)

    with open(resolved_path, "rb") as f:
        data = f.read()

    if ext in SUPPORTED_IMAGE_EXTS:
        image_key = await feishu.upload_image(data)
        msg_id = await feishu.send_image(chat_id, image_key)
    else:
        file_type = guess_file_type(ext)
        file_key = await feishu.upload_file(data, file_name, file_type)
        msg_id = await feishu.send_file(chat_id, file_key, file_name)

    return msg_id


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "FeishuSendFile",
    "发送本地文件或图片到飞书用户（通过当前活跃会话的 chat_id）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def feishu_send_file(args: dict) -> dict:
    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}

    # 获取 chat_id
    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书会话，请先在飞书里发一条消息"}],
            "is_error": True,
        }

    # 验证所有文件
    errors = []
    for fp in file_paths:
        if not os.path.exists(fp):
            errors.append(f"文件不存在: {fp}")
        elif os.path.getsize(fp) > MAX_FILE_SIZE:
            errors.append(f"{os.path.basename(fp)} 超过 30MB 限制")
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


# ── chat members tool ─────────────────────────────────────────────────────────

FEISHU_CHAT_MEMBERS_GUIDE = """
【飞书群聊艾特】当需要艾特群里的某个用户时，调用 mcp__SuperCC__GetChatMembers()，MCP 自动从当前会话获取 chat_id，返回群内所有成员的 mention 格式。
"""


@tool(
    "GetChatMembers",
    "获取当前飞书群的所有成员及其 mention 格式。入参为空，MCP 自动从当前会话获取 chat_id。"
    "返回成员列表，每项包含 name（用户名）和 mention（飞书 mention 标签）。"
    "当需要艾特群友时，先调用此工具获取正确格式。",
    {},
)
async def get_chat_members(args: dict) -> dict:
    """获取当前群的成员列表及 mention 格式。"""
    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书群会话，请先在群聊里发一条消息"}],
            "is_error": True,
        }

    feishu = _get_feishu_client()
    members = await feishu.get_chat_members(chat_id)
    if not members:
        return {
            "content": [{"type": "text", "text": "无法获取群成员列表，可能 bot 未加入该群"}],
            "is_error": True,
        }

    lines = ["群成员列表："]
    for m in members:
        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", "") or ""
        name = getattr(m, "name", "") or getattr(m, "member_id", "") or str(m)
        if member_id and name:
            mention = f"<at user_id=\"{member_id}\">{name}</at>"
            lines.append(f"  {name}: {mention}")
        else:
            lines.append(f"  {name or str(m)}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}

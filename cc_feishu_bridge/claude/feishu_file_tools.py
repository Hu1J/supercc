"""Feishu 文件发送 MCP 工具 — 暴露 FeishuSendFile 给 Claude Code 使用。"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


def _resolve_path(file_path: str) -> str:
    """将相对路径尝试解析为绝对路径。

    尝试顺序：原始路径 → 当前工作目录 → bridge 配置的 approved_directory。
    只在相对路径且原路径不存在时才尝试解析。
    """
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
    if os.path.exists(file_path):
        return os.path.abspath(file_path)

    # 相对路径：尝试从 config 里的 approved_directory 解析
    candidates = []
    try:
        from cc_feishu_bridge.config import load_config, resolve_config_path
        cfg_path, data_dir = resolve_config_path()
        config = load_config(cfg_path)
        approved = config.claude.approved_directory
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

FEISHU_FILE_GUIDE = """
【飞书文件发送】当用户要求发送文件/图片/截图/压缩包时，调用 mcp__feishu_file__FeishuSendFile(file_paths: list[str])，MCP 自动从当前会话获取 chat_id。
"""


def _get_feishu_client() -> "FeishuClient":
    """延迟初始化 FeishuClient（读取 config.yaml）。"""
    from cc_feishu_bridge.config import load_config, resolve_config_path
    from cc_feishu_bridge.feishu.client import FeishuClient
    cfg_path, _ = resolve_config_path()
    config = load_config(cfg_path)
    return FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
    )


def _get_session_manager() -> "SessionManager":
    """延迟初始化 SessionManager。"""
    from cc_feishu_bridge.config import resolve_config_path
    from cc_feishu_bridge.claude.session_manager import SessionManager
    _, data_dir = resolve_config_path()
    db_path = os.path.join(data_dir, "sessions.db")
    return SessionManager(db_path=db_path)


def _get_chat_id() -> Optional[str]:
    """从当前活跃会话获取 chat_id。"""
    sm = _get_session_manager()
    session = sm.get_active_session_by_chat_id()
    return session.chat_id if session else None


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件，返回 msg_id 或抛出异常。"""
    from cc_feishu_bridge.feishu.media import guess_file_type
    from cc_feishu_bridge.feishu.client import FeishuClient

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


def _build_feishu_file_mcp_server():
    from claude_agent_sdk import tool, create_sdk_mcp_server

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

    return create_sdk_mcp_server(
        name="feishu_file",
        version="1.0.0",
        tools=[feishu_send_file],
    )


_mcp_server = None
_mcp_server_lock = threading.Lock()


def get_feishu_file_mcp_server():
    global _mcp_server
    if _mcp_server is None:
        with _mcp_server_lock:
            if _mcp_server is None:
                _mcp_server = _build_feishu_file_mcp_server()
    return _mcp_server
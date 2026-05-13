"""Feishu MCP 工具 — 暴露飞书特有工具给 core 调用。

工具通过全局 _feishu_client 执行，该 client 由 mcp_server.py 的 main() 初始化。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from claude_agent_sdk import tool

# 全局 FeishuClient，由 mcp_server.main() 初始化
_feishu_client: Optional["FeishuClient"] = None
_current_chat_id: Optional[str] = None  # MCP 调用时的当前 chat_id


def init(feishu_client: "FeishuClient", chat_id: str | None = None):
    """初始化全局 FeishuClient 和 chat_id。"""
    global _feishu_client, _current_chat_id
    _feishu_client = feishu_client
    _current_chat_id = chat_id


def set_chat_id(chat_id: str):
    """设置当前 chat_id（每次 MCP 调用前由 core 设置）。"""
    global _current_chat_id
    _current_chat_id = chat_id


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


# ── 路径解析 ──────────────────────────────────────────────────────────────────

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


# ── 相对时间解析 ──────────────────────────────────────────────────────────────

def _parse_relative_time(s: str) -> Optional[int]:
    """将相对时间字符串解析为 Unix 时间戳。"""
    s = s.strip().lower()
    m = re.match(r"^(\d+)([hdmw])$", s)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    now = int(time.time())
    multipliers = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}
    return now - value * multipliers.get(unit, 0)


# ── 消息格式化 ────────────────────────────────────────────────────────────────

def _format_messages(items: list, keyword: str = "") -> str:
    """将 Feishu 消息列表格式化为可读文本。"""
    if not items:
        return "（无消息）"

    import datetime

    lines = []
    for msg in items:
        if isinstance(msg, dict):
            msg_type = msg.get("msg_type", "")
            content_str = msg.get("content", "{}")
            sender = msg.get("sender", {})
            sender_id = sender.get("id", "") if isinstance(sender, dict) else ""
            create_time = msg.get("create_time", "")
        else:
            msg_type = getattr(msg, "msg_type", "") or ""
            body = getattr(msg, "body", None)
            content_str = getattr(body, "content", "{}") if body else "{}"
            sender = getattr(msg, "sender", None)
            sender_id = getattr(sender, "id", "") if sender else ""
            create_time = getattr(msg, "create_time", "") or ""

        try:
            content = json.loads(content_str)
        except Exception:
            content = {"text": content_str}

        if msg_type == "text":
            text = content.get("text", "")
        elif msg_type == "post":
            text = content.get("text", "")
        elif msg_type == "image":
            text = "[图片]"
        elif msg_type == "file":
            fname = content.get("file_name", "文件") if isinstance(content, dict) else "文件"
            text = f"[文件: {fname}]"
        elif msg_type == "audio":
            text = "[语音消息]"
        elif msg_type == "video":
            text = "[视频消息]"
        elif msg_type == "sticker":
            text = "[表情消息]"
        else:
            text = str(content) if content else "[未知消息类型]"

        if keyword and keyword.lower() not in text.lower():
            continue

        if create_time:
            try:
                ts = int(create_time)
                dt = datetime.datetime.fromtimestamp(
                    ts, tz=datetime.timezone(datetime.timedelta(hours=8))
                )
                time_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                time_str = create_time
        else:
            time_str = ""

        lines.append(f"[{time_str}] {sender_id}: {text}")

    if not lines:
        return "（无匹配消息）"
    return "\n".join(lines)


# ── 工具定义 ──────────────────────────────────────────────────────────────────

@tool(
    "FeishuSendFile",
    "发送本地文件或图片到飞书（通过当前活跃会话的 chat_id）。"
    "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
    "每个文件需在 30MB 以内。",
    {"file_paths": list},
)
async def feishu_send_file(args: dict) -> dict:
    """发送本地文件到飞书。"""
    if _feishu_client is None:
        return {"content": [{"type": "text", "text": "FeishuClient not initialized"}], "is_error": True}

    file_paths: list = args.get("file_paths", [])
    if not file_paths:
        return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}

    chat_id = _current_chat_id
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书会话，请先在飞书里发一条消息"}],
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
            file_name = os.path.basename(resolved)
            with open(resolved, "rb") as f:
                data = f.read()

            if ext in SUPPORTED_IMAGE_EXTS:
                image_key = await _feishu_client.upload_image(data)
                msg_id = await _feishu_client.send_image(chat_id, image_key)
            else:
                from supercc.adapter.feishu.media import guess_file_type
                file_type = guess_file_type(ext)
                file_key = await _feishu_client.upload_file(data, file_name, file_type)
                msg_id = await _feishu_client.send_file(chat_id, file_key, file_name)
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


@tool(
    "GetChatMembers",
    "获取当前飞书群的所有成员及其 mention 格式。入参为空，MCP 自动从当前会话获取 chat_id。"
    "返回成员列表，每项包含 name（用户名）和 mention（飞书 mention 标签）。"
    "当需要艾特群友时，先调用此工具获取正确格式。",
    {},
)
async def get_chat_members(args: dict) -> dict:
    """获取当前群的成员列表及 mention 格式。"""
    if _feishu_client is None:
        return {"content": [{"type": "text", "text": "FeishuClient not initialized"}], "is_error": True}

    chat_id = _current_chat_id
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书群会话，请先在群聊里发一条消息"}],
            "is_error": True,
        }

    members = await _feishu_client.get_chat_members(chat_id)
    if not members:
        return {
            "content": [{"type": "text", "text": "无法获取群成员列表，可能 bot 未加入该群"}],
            "is_error": True,
        }

    lines = ["群成员列表："]
    for m in members:
        member_id = getattr(m, "member_id", None) or getattr(m, "open_id", None)
        name = getattr(m, "name", None)
        if isinstance(m, dict):
            member_id = member_id or m.get("bot_id")
            name = name or m.get("bot_name")
        if member_id and name:
            mention = f"<at user_id=\"{member_id}\">{name}</at>"
            lines.append(f"  {name}: {mention}")
        else:
            lines.append(f"  {name or str(m)}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "FeishuChatHistory",
    "检索飞书群聊的历史消息。"
    "支持按时间范围筛选（start_time/end_time 为 Unix 时间戳或相对时间如 '1h'/'1d'/'7d'），"
    "以及关键词过滤（客户端筛选）。"
    "返回格式化的消息列表，包含发送者、时间和内容。"
    "注意：飞书 API 每次最多返回 20 条，超过需调整 start_time 重新拉取。",
    {
        "keyword": str,
        "limit": int,
        "start_time": str,
        "end_time": str,
        "direction": str,
    },
)
async def feishu_chat_history(args: dict) -> dict:
    """检索当前飞书群聊的历史消息。"""
    if _feishu_client is None:
        return {"content": [{"type": "text", "text": "FeishuClient not initialized"}], "is_error": True}

    chat_id = _current_chat_id
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书群会话，请先在群聊里发一条消息"}],
            "is_error": True,
        }

    keyword = args.get("keyword", "").strip()
    limit = args.get("limit", 20)
    limit = max(1, min(limit, 200))
    start_time_str = args.get("start_time", "").strip()
    end_time_str = args.get("end_time", "").strip()
    direction = args.get("direction", "newest_first").strip()

    start_time: Optional[int] = None
    end_time: Optional[int] = None

    if start_time_str:
        try:
            start_time = int(start_time_str)
        except ValueError:
            start_time = _parse_relative_time(start_time_str)

    if end_time_str:
        try:
            end_time = int(end_time_str)
        except ValueError:
            end_time = _parse_relative_time(end_time_str)

    sort_type = "ByCreateTimeDesc" if direction == "newest_first" else "ByCreateTimeAsc"

    try:
        fetch_count = min(limit, 20)
        resp = await _feishu_client.get_chat_history(
            chat_id=chat_id,
            limit=fetch_count,
            sort_type=sort_type,
        )
        all_items = resp if isinstance(resp, list) else []
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"获取历史消息失败: {e}"}],
            "is_error": True,
        }

    # 时间范围过滤
    filtered = []
    for msg in all_items:
        ts_str = msg.get("create_time", "") if isinstance(msg, dict) else getattr(msg, "create_time", "") or ""
        if ts_str:
            try:
                ts = int(ts_str)
                if start_time and ts < start_time:
                    if direction == "newest_first":
                        break
                    continue
                if end_time and ts > end_time:
                    if direction != "newest_first":
                        continue
                    continue
            except Exception:
                pass
        filtered.append(msg)

    formatted = _format_messages(filtered, keyword=keyword)
    has_more = len(all_items) >= 20

    result_text = f"【群聊历史】共 {len(filtered)} 条消息匹配（最多显示 {limit} 条）\n\n{formatted}"
    if has_more:
        result_text += f"\n\n⚠️ 消息较多，超出飞书 API 单次限制（20条）。可通过调整 start_time 继续获取更早的消息。"

    return {"content": [{"type": "text", "text": result_text}]}

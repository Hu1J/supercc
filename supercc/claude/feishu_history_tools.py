"""Feishu 群聊历史消息检索 MCP 工具 — 暴露 FeishuChatHistory 给 Claude Code 使用。"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from claude_agent_sdk import tool
from supercc.channels.feishu.client import FeishuClient


def _parse_relative_time(s: str) -> Optional[int]:
    """将相对时间字符串解析为 Unix 时间戳。

    支持格式：
      1h  → 当前时间 - 1 小时
      1d  → 当前时间 - 1 天
      7d  → 当前时间 - 7 天
      30d → 当前时间 - 30 天
    返回 None 表示解析失败。
    """
    s = s.strip().lower()
    m = re.match(r"^(\d+)([hdmw])$", s)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    now = int(time.time())
    multipliers = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}
    return now - value * multipliers.get(unit, 0)


def _get_feishu_client() -> "FeishuClient":
    """延迟初始化 FeishuClient（读取 config.yaml）。"""
    from supercc.config import get_config
    from supercc.channels.feishu.client import FeishuClient
    cfg = get_config()
    return FeishuClient(
        app_id=cfg.channels.feishu.app_id,
        app_secret=cfg.channels.feishu.app_secret,
    )


def _get_chat_id() -> Optional[str]:
    """从 contextvar 获取当前 chat_id。"""
    from supercc.claude.message_context import get_current_chat_id
    return get_current_chat_id()


def _format_messages(items: list, keyword: str = "") -> str:
    """将 Feishu 消息列表格式化为可读文本。

    对每条消息提取：发送者ID、发送时间、消息内容。
    如果指定了 keyword，则只保留内容包含关键词的消息。
    """
    if not items:
        return "（无消息）"

    import datetime

    lines = []
    for msg in items:
        # 兼容 dict 和 lark-oapi 对象
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

        # 解析消息内容
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

        # 关键词过滤（客户端）
        if keyword and keyword.lower() not in text.lower():
            continue

        # 格式化时间
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


# ── MCP tool ──────────────────────────────────────────────────────────────────

FEISHU_CHAT_HISTORY_GUIDE = """
【飞书群聊历史】当主人想查看飞书群聊的历史消息时，调用 mcp__SuperCC__FeishuChatHistory。
支持的参数：
- keyword: 关键词过滤（只返回包含该关键词的消息）
- limit: 最大返回消息条数（默认 20，最多 200）
- start_time: 开始时间，支持 Unix 时间戳或相对时间如 "1h"（1小时前）、"7d"（7天前）
- end_time: 结束时间，支持 Unix 时间戳或相对时间如 "1d"（1天前）
- direction: "newest_first"（默认，最新消息在前）或 "oldest_first"（最老的消息在前）

注意：飞书 ListMessage API 暂不支持 page_token 分页，每次最多返回 20 条。超过 20 条时需要重新传入不同的 start_time 来继续获取更早的消息。

示例：查看最近1天的所有消息
mcp__SuperCC__FeishuChatHistory({"start_time": "1d", "limit": 50})

示例：搜索包含"关键词"的消息
mcp__SuperCC__FeishuChatHistory({"keyword": "关键词", "limit": 50})
"""


@tool(
    "FeishuChatHistory",
    "检索飞书群聊的历史消息。"
    "支持按时间范围筛选（start_time/end_time 为 Unix 时间戳或相对时间如 '1h'/'1d'/'7d'），"
    "以及关键词过滤（客户端筛选）。"
    "返回格式化的消息列表，包含发送者、时间和内容。"
    "注意：飞书 API 每次最多返回 20 条，超过需调整 start_time 重新拉取。",
    {
        "keyword": str,       # 关键词过滤（客户端筛选）
        "limit": int,         # 最大消息条数，默认 20，最多 200
        "start_time": str,    # 开始时间（Unix 时间戳或相对时间如 '1h'）
        "end_time": str,      # 结束时间（Unix 时间戳或相对时间如 '1d'）
        "direction": str,     # "newest_first"（默认）或 "oldest_first"
    },
)
async def feishu_chat_history(args: dict) -> dict:
    """检索当前飞书群聊的历史消息。"""
    chat_id = _get_chat_id()
    if not chat_id:
        return {
            "content": [{"type": "text", "text": "未找到活跃飞书群会话，请先在群聊里发一条消息"}],
            "is_error": True,
        }

    keyword = args.get("keyword", "").strip()
    limit = args.get("limit", 20)
    limit = max(1, min(limit, 200))  # 限制在 1-200
    start_time_str = args.get("start_time", "").strip()
    end_time_str = args.get("end_time", "").strip()
    direction = args.get("direction", "newest_first").strip()

    # 解析时间
    start_time: Optional[int] = None
    end_time: Optional[int] = None

    if start_time_str:
        # 先尝试直接解析为 Unix 时间戳
        try:
            start_time = int(start_time_str)
        except ValueError:
            start_time = _parse_relative_time(start_time_str)

    if end_time_str:
        try:
            end_time = int(end_time_str)
        except ValueError:
            end_time = _parse_relative_time(end_time_str)

    # 排序方向
    sort_type = "ByCreateTimeDesc" if direction == "newest_first" else "ByCreateTimeAsc"

    feishu = _get_feishu_client()

    try:
        all_items = []
        # 注意：Feishu ListMessage API 暂不支持 page_token 分页参数，
        # 每次调用只返回一页（最多 20 条），超过需手动多次调用
        fetch_count = min(limit, 20)
        resp = await feishu.get_chat_history(
            chat_id=chat_id,
            limit=fetch_count,
            sort_type=sort_type,
        )

        if not resp:
            all_items = []
        else:
            all_items = resp if isinstance(resp, list) else []

    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"获取历史消息失败: {e}"}],
            "is_error": True,
        }

    # 时间范围过滤
    # - start_time: 保留 ts >= start_time（不早于开始时间）
    # - end_time:   保留 ts <= end_time（不晚于结束时间）
    # 降序时 start_time 的 break 优化是安全的：一旦遇到 ts < start_time，
    # 后续所有消息都更旧，不可能再满足 start_time 条件。
    # 但 end_time 的 break 优化不安全，因为降序中遇到 ts > end_time 时，
    # 后续消息可能落在 [start_time, end_time] 范围内，因此 end_time 只能用 continue 跳过。
    filtered = []
    for msg in all_items:
        if isinstance(msg, dict):
            ts_str = msg.get("create_time", "")
        else:
            ts_str = getattr(msg, "create_time", "") or ""
        if ts_str:
            try:
                ts = int(ts_str)
                if start_time and ts < start_time:
                    if direction == "newest_first":
                        # 降序：遇到比 start_time 还老的消息，后续都更老，直接停止
                        break
                    # 升序：继续往后可能有更新的消息
                    continue
                if end_time and ts > end_time:
                    if direction != "newest_first":
                        # 升序：遇到比 end_time 还新的消息，后续都更新，跳过
                        continue
                    # 降序：消息是新的在前，继续往后可能有落在范围内的
                    continue
            except Exception:
                pass
        filtered.append(msg)

    # 关键词过滤
    formatted = _format_messages(filtered, keyword=keyword)

    # 是否还有更多：API 每次最多返回 20 条，如果这次拿到了 20 条就说明可能有更多
    has_more = len(all_items) >= 20

    result_text = f"【群聊历史】共 {len(filtered)} 条消息匹配（最多显示 {limit} 条）\n\n{formatted}"
    if has_more:
        result_text += f"\n\n⚠️ 消息较多，超出飞书 API 单次限制（20条）。可通过调整 start_time 继续获取更早的消息。"

    return {"content": [{"type": "text", "text": result_text}]}

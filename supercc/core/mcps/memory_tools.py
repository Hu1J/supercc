"""Memory MCP tools — 10 tools, one per /memory command."""
from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import tool
from supercc.core.memory_manager import get_memory_manager
from supercc.core.message_context import get_current_bot_id, get_current_chat_id, get_current_platform, get_current_user_open_id

logger = logging.getLogger(__name__)


def _fmt_pref(p) -> str:
    return "\n".join([
        f"[用户偏好] **{p.title}**",
        f"  {p.content}",
        f"  关键词: {p.keywords}",
        f"  ID: `{p.id}`",
    ])


def _fmt_proj(m) -> str:
    proj = m.project_path or "(未知项目)"
    return "\n".join([
        f"[项目记忆] **{m.title}**",
        f"  {m.content}",
        f"  关键词: {m.keywords}",
        f"  项目: {proj}",
        f"  ID: `{m.id}`",
    ])


def _get_user_open_id() -> str | None:
    """从当前消息上下文获取 user_open_id（通过 contextvar）。"""
    return get_current_user_open_id()


# ── user tools ─────────────────────────────────────────────────────────────────

@tool(
    "MemoryAddUser",
    "新增一条用户偏好（自动使用当前飞书用户身份）。title + content + keywords 三样必填，关键词用逗号分隔。",
    {"title": str, "content": str, "keywords": str},
)
async def memory_add_user(args: dict) -> dict:
    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    keywords = args.get("keywords", "").strip()
    user_open_id = args.get("user_open_id", "").strip() or _get_user_open_id()
    if not title or not content or not keywords:
        return {"content": [{"type": "text", "text": "title、content、keywords 三样必填"}], "is_error": True}
    if not user_open_id:
        return {"content": [{"type": "text", "text": "无法获取当前用户身份，请确保在飞书私聊中使用"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    bot_id = get_current_bot_id() or ""
    try:
        p = mm.add_preference(user_open_id, title, content, keywords, platform=platform, bot_id=bot_id)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"输入过长：{e}"}], "is_error": True}
    return {"content": [{"type": "text", "text": f"✅ 用户偏好已保存\n\n{_fmt_pref(p)}"}]}


@tool(
    "MemoryDeleteUser",
    "删除指定 ID 的用户偏好。",
    {"id": str},
)
async def memory_delete_user(args: dict) -> dict:
    mm = get_memory_manager()
    platform = get_current_platform()
    user_open_id = _get_user_open_id()
    bot_id = get_current_bot_id() or ""
    if not user_open_id:
        return {"content": [{"type": "text", "text": "无法获取当前用户身份"}], "is_error": True}
    ok = mm.delete_preference(args["id"], user_open_id=user_open_id, platform=platform, bot_id=bot_id)
    if ok:
        return {"content": [{"type": "text", "text": f"🗑️ 用户偏好 {args['id']} 已删除。"}]}
    return {"content": [{"type": "text", "text": f"未找到 id={args['id']} 的用户偏好"}], "is_error": True}


@tool(
    "MemoryUpdateUser",
    "更新指定 ID 的用户偏好。title + content + keywords 三样必填，关键词用逗号分隔。",
    {"id": str, "title": str, "content": str, "keywords": str},
)
async def memory_update_user(args: dict) -> dict:
    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    keywords = args.get("keywords", "").strip()
    if not title or not content or not keywords:
        return {"content": [{"type": "text", "text": "title、content、keywords 三样必填"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    user_open_id = _get_user_open_id()
    bot_id = get_current_bot_id() or ""
    if not user_open_id:
        return {"content": [{"type": "text", "text": "无法获取当前用户身份"}], "is_error": True}
    ok = mm.update_preference(args["id"], title, content, keywords, user_open_id=user_open_id, platform=platform, bot_id=bot_id)
    if ok:
        return {"content": [{"type": "text", "text": f"✅ 用户偏好 {args['id']} 已更新。"}]}
    return {"content": [{"type": "text", "text": f"未找到 id={args['id']} 的用户偏好"}], "is_error": True}


@tool(
    "MemoryListUser",
    "列出当前用户的所有偏好（自动使用当前飞书用户身份）。",
    {},
)
async def memory_list_user(args: dict) -> dict:
    user_open_id = _get_user_open_id()
    platform = get_current_platform()
    bot_id = get_current_bot_id() or ""
    logger.debug(f"[MemoryListUser] user_open_id={user_open_id}, platform={platform}, bot_id={bot_id!r}")
    mm = get_memory_manager()
    if user_open_id:
        prefs = mm.get_preferences_by_user(user_open_id, platform=platform, bot_id=bot_id)
    else:
        prefs = []
    if not prefs:
        return {"content": [{"type": "text", "text": "📭 暂无用户偏好记录。"}]}
    lines = [f"👤 用户偏好（共 {len(prefs)} 条）\n"]
    for p in prefs:
        lines.append(_fmt_pref(p))
        lines.append("")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "MemorySearchUser",
    "搜索用户偏好（全文检索，自动使用当前飞书用户身份）。",
    {"query": str},
)
async def memory_search_user(args: dict) -> dict:
    query = args.get("query", "").strip()
    user_open_id = args.get("user_open_id", "").strip() or _get_user_open_id()
    if not user_open_id:
        return {"content": [{"type": "text", "text": f"未找到与「{query}」相关的用户偏好。"}]}
    if not query:
        return {"content": [{"type": "text", "text": "查询词不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    bot_id = get_current_bot_id() or ""
    results = mm.search_preferences(query, user_open_id=user_open_id, platform=platform, bot_id=bot_id, limit=5)
    if not results:
        return {"content": [{"type": "text", "text": f"未找到与「{query}」相关的用户偏好。"}]}
    lines = [f"🔍 用户偏好搜索结果（共 {len(results)} 条）\n"]
    for p in results:
        lines.append(_fmt_pref(p))
        lines.append("")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


# ── proj tools ─────────────────────────────────────────────────────────────────

@tool(
    "MemoryAddProj",
    "新增一条项目记忆（语义搜索，按项目隔离）。title + content + keywords 三样必填，关键词用逗号分隔。",
    {"project_path": str, "title": str, "content": str, "keywords": str},
)
async def memory_add_proj(args: dict) -> dict:
    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    keywords = args.get("keywords", "").strip()
    project_path = args.get("project_path", "").strip()
    if not title or not content or not keywords:
        return {"content": [{"type": "text", "text": "title、content、keywords 三样必填"}], "is_error": True}
    if not project_path:
        return {"content": [{"type": "text", "text": "project_path 不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    chat_id = get_current_chat_id() or ""
    try:
        m = mm.add_project_memory(project_path, title, content, keywords, platform=platform, chat_id=chat_id)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"输入过长：{e}"}], "is_error": True}
    return {"content": [{"type": "text", "text": f"✅ 项目记忆已保存\n\n{_fmt_proj(m)}"}]}


@tool(
    "MemoryDeleteProj",
    "删除指定 ID 的项目记忆。",
    {"id": str, "project_path": str},
)
async def memory_delete_proj(args: dict) -> dict:
    project_path = args.get("project_path", "").strip()
    if not project_path:
        return {"content": [{"type": "text", "text": "project_path 不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    chat_id = get_current_chat_id() or ""
    deleted = mm.delete_project_memory(args["id"], project_path, platform=platform, chat_id=chat_id)
    if deleted:
        return {"content": [{"type": "text", "text": f"🗑️ 项目记忆 {deleted['id']} 已删除。"}]}
    return {"content": [{"type": "text", "text": f"未找到 id={args['id']} 的项目记忆"}], "is_error": True}


@tool(
    "MemoryUpdateProj",
    "更新指定 ID 的项目记忆（按项目隔离）。title + content + keywords 三样必填，关键词用逗号分隔。",
    {"id": str, "title": str, "content": str, "keywords": str, "project_path": str},
)
async def memory_update_proj(args: dict) -> dict:
    title = args.get("title", "").strip()
    content = args.get("content", "").strip()
    keywords = args.get("keywords", "").strip()
    project_path = args.get("project_path", "").strip()
    if not title or not content or not keywords:
        return {"content": [{"type": "text", "text": "title、content、keywords 三样必填"}], "is_error": True}
    if not project_path:
        return {"content": [{"type": "text", "text": "project_path 不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    chat_id = get_current_chat_id() or ""
    ok = mm.update_project_memory(args["id"], title, content, keywords, project_path, platform=platform, chat_id=chat_id)
    if ok:
        return {"content": [{"type": "text", "text": f"✅ 项目记忆 {args['id']} 已更新。"}]}
    return {"content": [{"type": "text", "text": f"未找到 id={args['id']} 的项目记忆"}], "is_error": True}


@tool(
    "MemoryListProj",
    "列出指定项目下所有项目记忆（语义搜索，按项目隔离）。",
    {"project_path": str},
)
async def memory_list_proj(args: dict) -> dict:
    project_path = args.get("project_path", "").strip()
    if not project_path:
        return {"content": [{"type": "text", "text": "project_path 不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    chat_id = get_current_chat_id() or ""
    mems = mm.get_project_memories(project_path, platform=platform, chat_id=chat_id)
    if not mems:
        return {"content": [{"type": "text", "text": "📭 暂无项目记忆记录。"}]}
    lines = [f"📁 项目记忆（共 {len(mems)} 条）\n"]
    for m in mems:
        lines.append(_fmt_proj(m))
        lines.append("")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "MemorySearchProj",
    "搜索项目记忆（语义+关键词混合搜索）。按项目隔离，只搜当前项目。",
    {"query": str, "project_path": str},
)
async def memory_search_proj(args: dict) -> dict:
    query = args.get("query", "").strip()
    project_path = args.get("project_path", "").strip()
    if not query or not project_path:
        return {"content": [{"type": "text", "text": "query 和 project_path 不能为空"}], "is_error": True}
    mm = get_memory_manager()
    platform = get_current_platform()
    chat_id = get_current_chat_id() or ""
    results = mm.search_project_memories(query, project_path, platform=platform, chat_id=chat_id, limit=5)
    if not results:
        return {"content": [{"type": "text", "text": f"未找到与「{query}」相关的项目记忆。"}]}
    lines = [f"🔍 项目记忆搜索结果（共 {len(results)} 条）\n"]
    for r in results:
        lines.append(_fmt_proj(r.memory))
        lines.append("")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}

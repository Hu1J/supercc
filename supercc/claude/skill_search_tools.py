"""Skill Search MCP tools — exposed to Claude Code via MCP."""
from __future__ import annotations

from claude_agent_sdk import tool
from supercc.skill_search import get_skill_search_registry
from supercc.skill_search.models import SkillMeta


SKILL_SEARCH_GUIDE = """
【Skill 搜索】工具前缀: mcp__SuperCC__

当用户要求搜索 Claude Code Skill 时使用：

## Skill 搜索工具
mcp__SuperCC__SkillSearch — 搜索网上 Claude Code Skill（并行查询多个来源）

输入 query 即可，内部自动判断是语义搜索还是名字搜索。
"""


def _fmt_skill(meta: SkillMeta) -> str:
    tags_str = ", ".join(meta.tags) if meta.tags else "无"
    return "\n".join([
        f"  🏷️ **{meta.name}** (信任: {meta.trust_level})",
        f"  描述: {meta.description or '无'}",
        f"  标签: {tags_str}",
        f"  来源: {meta.source}",
        f"  标识: {meta.identifier}",
    ])


def _fmt_results(results: list[SkillMeta], query: str) -> str:
    if not results:
        return f"未找到与「{query}」相关的 Skill。"

    lines = [f"🔍 搜索「{query}」结果（共 {len(results)} 条）\n"]
    current_source = None
    for r in results:
        if r.source != current_source:
            lines.append(f"\n📦 {r.source}\n")
            current_source = r.source
        lines.append(_fmt_skill(r))
        lines.append("")
    return "\n".join(lines)


# ── tool ──────────────────────────────────────────────────────────────────────

@tool(
    "SkillSearch",
    "在网上搜索 Claude Code Skill（语义搜索或按名字搜索），并行查询多个来源："
    "skills.sh, GitHub (openai/anthropics/VoltAgent), Hermes Index, "
    "ClawHub, Claude Marketplace, LobeHub, Well-Known Skills。",
    {"query": str, "limit": int},
)
async def skill_search(args: dict) -> dict:
    query = args.get("query", "").strip()
    limit = args.get("limit", 5)
    if not query:
        return {"content": [{"type": "text", "text": "query 不能为空"}], "is_error": True}

    registry = get_skill_search_registry()
    results = await registry.search_all(query, limit_per_source=limit)
    return {"content": [{"type": "text", "text": _fmt_results(results, query)}]}


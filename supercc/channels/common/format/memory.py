"""记忆工具结果卡片 — 各平台通用 markdown 渲染。"""
from __future__ import annotations

import json as _json


class MemoryCardMarker:
    """记忆工具结果卡片标记，输出纯 markdown（各平台通用）。

    card_type 决定 render() 如何格式化：
    - add / update  → 参数表格（标题列置顶）
    - delete        → 删除条目 ID 表格
    - list / search → 实际记忆条目表格（需查库后传入 entries）
    """
    __slots__ = ("tool_name", "card_type", "entries", "tool_input")

    def __init__(self, tool_name: str, card_type: str, entries: list, tool_input: str):
        self.tool_name = tool_name      # 原始工具名，如 "mcp__SuperCC__MemoryAddProj"
        self.card_type = card_type      # add | update | delete | list | search
        self.entries = entries          # list[dict] — 查库后的实际条目
        self.tool_input = tool_input    # 原始 JSON 入参

    def render(self) -> str:
        """渲染为纯 markdown 字符串，各平台通用。"""
        try:
            args = _json.loads(self.tool_input) if self.tool_input else {}
        except _json.JSONDecodeError:
            args = {}

        short = self.tool_name.replace("mcp__SuperCC__", "")
        scope = "proj" if "Proj" in short else "user"
        card_type = self.card_type or ""

        header = f"🧠 **{short}**"
        if card_type == "search":
            q = args.get("query", "")
            header += f"  查询: 「{q}」"
        if scope == "proj":
            pp = args.get("project_path", "")
            if pp:
                header += f"  项目: {pp.split('/')[-1] or pp}"
        elif args.get("user_open_id"):
            header += f"  用户: {args['user_open_id']}"

        def _esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

        if card_type in ("add", "update"):
            if not self.entries:
                return f"{header}\n\n_无结果_"
            lines = f"{header}\n\n| 标题 | 内容摘要 | 关键词 |\n|------|----------|--------|\n"
            for e in self.entries:
                title = _esc(e.get("title", "")[:60])
                content = _esc(e.get("content", "")[:50])
                keywords = _esc(e.get("keywords", ""))
                lines += f"| {title} | {content} | {keywords} |\n"
            return lines

        elif card_type in ("list", "search"):
            total = len(self.entries)
            header += f"（共 {total} 条）"
            if not self.entries:
                return f"{header}\n\n_无结果_"
            lines = f"{header}\n\n| # | 标题 | 内容摘要 | 关键词 | ID |\n|---|------|----------|--------|---|\n"
            for i, e in enumerate(self.entries, 1):
                title = _esc(e.get("title", "")[:40])
                content = _esc(e.get("content", "")[:50])
                keywords = _esc(e.get("keywords", ""))
                mid = f"`{e.get('id', '')}`"
                lines += f"| {i} | {title} | {content} | {keywords} | {mid} |\n"
            return lines

        elif card_type == "delete":
            deleted_id = self.entries[0].get("id", "") if self.entries else ""
            return f"{header}\n\n| ID |\n|------|\n| `{deleted_id}` |\n"

        # fallback: 参数表
        lines = f"{header}\n\n| 参数 | 值 |\n|------|----|\n"
        for k, v in args.items():
            v_str = _esc(str(v))
            if len(v_str) > 80:
                v_str = v_str[:80] + "…"
            lines += f"| `{k}` | {v_str} |\n"
        return lines

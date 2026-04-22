"""Format Claude's Markdown response for Feishu."""
from __future__ import annotations

import json
import re

from cc_feishu_bridge.format.edit_diff import build_edit_marker, build_write_marker, _DiffMarker, _MemoryCardMarker
from cc_feishu_bridge.format.questionnaire_card import _AskUserQuestionMarker

FEISHU_MAX_MESSAGE_LENGTH = 4096
# Feishu CardKit limit for markdown tables per card
FEISHU_CARD_TABLE_LIMIT = 230099

# Placeholder marker for protected code blocks during markdown optimization
_CODE_BLOCK_MARK = "___CB_"
_CODE_BLOCK_MARK_END = "___"


def optimize_markdown_style(text: str, card_version: int = 2) -> str:
    """Optimize Markdown for Feishu rendering (port of markdown-style.js).

    - Headings: H1 → H4, H2~H6 → H5
    - Table spacing: adds <br> before/after tables
    - Code blocks: wrapped with <br> for separation
    - Strips non-img_ image URLs (Feishu CardKit only accepts img_xxx keys)
    """
    try:
        text = _optimize_markdown_style_impl(text, card_version)
        text = _strip_invalid_image_keys(text)
        return text
    except Exception:
        return text


def _optimize_markdown_style_impl(text: str, card_version: int = 2) -> str:
    # 1. Protect code blocks with placeholders
    code_blocks: list[str] = []
    r = re.sub(
        r"```[\s\S]*?```",
        lambda m: f"{_CODE_BLOCK_MARK}{len(code_blocks)}{_CODE_BLOCK_MARK_END}",
    )

    # 2. Heading level reduction (only if H1-H3 exist in original)
    if re.search(r"^#{1,3} ", text, re.MULTILINE):
        r = re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=re.MULTILINE)
        r = re.sub(r"^# (.+)$", r"#### \1", r, flags=re.MULTILINE)

    if card_version >= 2:
        # 3. Spacing between consecutive headings
        r = re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", r, flags=re.MULTILINE)

        # 4. Table spacing
        # 4a: non-table line directly before table row → add blank line first
        r = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", r, flags=re.MULTILINE)
        # 4b: add <br> before table
        r = re.sub(r"\n\n((?:\|.+\|[^\S\n]*\n?)+)", r"\n\n<br>\n\n\1", r)
        # 4c: add <br> after table
        r = re.sub(r"((?:^\|.+\|[^\S\n]*\n?)+)", r"\1\n<br>\n", r, flags=re.MULTILINE)
        # 4d: reduce extra blank lines when non-heading/non-bold text precedes table
        r = re.sub(
            r"^((?!#{4,5} )(?!\*\*).+)\n\n(<br>)\n\n(\|)",
            r"\1\n\2\n\3",
            r,
            flags=re.MULTILINE,
        )
        # 4d2: bold text before table — keep blank line after bold
        r = re.sub(
            r"^(\*\*.+)\n\n(<br>)\n\n(\|)",
            r"\1\n\2\n\n\3",
            r,
            flags=re.MULTILINE,
        )
        # 4e: reduce blank lines when non-heading/non-bold text follows table
        r = re.sub(
            r"(\|[^\n]*\n)\n(<br>\n)((?!#{4,5} )(?!\*\*))",
            r"\1\2\3",
            r,
        )

        # 5. Restore code blocks with <br> wrapping
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{_CODE_BLOCK_MARK}{i}{_CODE_BLOCK_MARK_END}", f"\n<br>\n{block}\n<br>\n")
    else:
        # 5. Restore code blocks (no <br>)
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{_CODE_BLOCK_MARK}{i}{_CODE_BLOCK_MARK_END}", block)

    # 6. Collapse 3+ consecutive newlines to 2
    r = re.sub(r"\n{3,}", r"\n\n", r)
    return r


def _strip_invalid_image_keys(text: str) -> str:
    """Strip markdown image syntax where URL is not a Feishu img_xxx key.

    Feishu CardKit only accepts img_xxx image keys (uploaded via media API).
    HTTP URLs and local paths in markdown images cause CardKit error 200570.
    We strip them so the text renders without the broken image.
    """
    if "!(" not in text:
        return text

    def _replacer(m: re.Match) -> str:
        url = m.group(2)
        # Keep only Feishu image keys (img_v3_xxx format)
        if url.startswith("img_"):
            return m.group(0)
        return ""

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _replacer, text)


def should_use_card(text: str) -> bool:
    """Decide whether to send as Feishu Interactive Card vs post.

    Cards are used for content with fenced code blocks or markdown tables
    (better rendering in a wide-screen card). Falls back to post if there
    are too many tables (CardKit limit).
    """
    table_count = _count_tables_outside_code_blocks(text)
    if table_count > FEISHU_CARD_TABLE_LIMIT:
        return False
    has_code = bool(re.search(r"```[\s\S]*?```", text))
    if has_code:
        return True
    if table_count > 0:
        return True
    return False


def _count_tables_outside_code_blocks(text: str) -> int:
    """Count markdown table rows that are not inside fenced code blocks."""
    # Remove fenced code blocks first
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    # Count lines that look like table rows: start with | and contain at least one more |
    lines = stripped.split("\n")
    count = 0
    for line in lines:
        line = line.strip()
        if line.startswith("|") and "|" in line[1:]:
            count += 1
    return max(0, count - 1)  # subtract header row


class ReplyFormatter:
    def __init__(self):
        self.tool_icons = {
            "Read": "📖",
            "Write": "✏️",
            "Edit": "🔧",
            "Bash": "💻",
            "Glob": "🔍",
            "Grep": "🔎",
            "WebFetch": "🌐",
            "WebSearch": "🌐",
            "Task": "📋",
            "MemorySearch": "🧠",
            "MemoryList": "🧠",
            "MemoryAdd": "🧠",
            "MemoryDelete": "🧠",
            "MemoryClear": "🧠",
        }

    def format_text(self, text: str) -> str:
        """Prepare Markdown text for Feishu post/card rendering.

        Strips non-Feishu image URLs (img_xxx keys only) and applies
        Feishu-specific style optimizations (heading levels, table spacing).
        Code blocks and tables are preserved intact.
        """
        if not text:
            return ""
        text = optimize_markdown_style(text, card_version=2)
        return text.strip()

    def format_tool_call(
        self,
        tool_name: str,
        tool_input: str | None = None,
        **kwargs,
    ) -> str | _DiffMarker | list[_DiffMarker] | _MemoryCardMarker:
        """Format a tool call notification for the user.

        Returns _DiffMarker for Edit/Write tools (to trigger colored card rendering),
        or a plain string for all other tools.
        """
        if tool_input is None:
            tool_input = ""

        # Edit / Write → 彩色 diff 卡片
        if tool_name == "Edit":
            if tool_input.strip():
                try:
                    return build_edit_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式
        elif tool_name == "Write":
            if tool_input.strip():
                try:
                    return build_write_marker(tool_input)
                except (json.JSONDecodeError, KeyError):
                    pass  # 降级到 backtick 格式

        # Bash → md 代码段，description 转注释
        elif tool_name == "Bash":
            return self._format_bash_tool(tool_input)

        # TodoWrite → markdown 表格
        elif tool_name == "TodoWrite":
            return self._format_todowrite_tool(tool_input)

        # AskUserQuestion → 精美问卷卡片
        elif tool_name == "AskUserQuestion":
            marker = _AskUserQuestionMarker(tool_name, tool_input)
            if marker.data is not None:
                return marker
            # 解析失败，降级为普通文本
            return f"🤖 **{tool_name}**\n`{tool_input}`"

        # Memory MCP tools → 卡片标记（触发 Feishu Interactive Card）
        elif tool_name and tool_name.startswith("mcp__memory__"):
            return self._format_memory_tool(
                tool_name, tool_input,
                memory_manager=kwargs.get("memory_manager"),
                default_project_path=kwargs.get("default_project_path", ""),
            )

        # Read → 提取 file_path，用 backtick 包裹
        elif tool_name == "Read":
            return self._format_read_tool(tool_input)

        # Cron MCP tools → ⏰ 时钟图标
        elif tool_name and tool_name.startswith("mcp__cron__"):
            icon = "⏰"
            short = tool_name.replace("mcp__cron__", "")
            msg = f"{icon} **{short}**"
            if tool_input and len(tool_input) <= FEISHU_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            return msg

        # 其他工具 → backtick 格式（原有逻辑）
        icon = self.tool_icons.get(tool_name, "🤖")
        msg = f"{icon} **{tool_name}**"
        if tool_input:
            if len(tool_input) <= FEISHU_MAX_MESSAGE_LENGTH - len(msg) - 5:
                msg += f"\n`{tool_input}`"
            else:
                chunks = self.split_messages(tool_input)
                for chunk in chunks:
                    msg += f"\n`{chunk}`"
        return msg

    # ── Memory MCP tool 格式化 ────────────────────────────────────────────────
    _MEM_PAGE_SIZE = 5

    def _format_memory_tool(
        self,
        tool_name: str,
        tool_input: str,
        memory_manager=None,
        default_project_path: str = "",
    ) -> _MemoryCardMarker | str:
        """格式化记忆 MCP 工具调用为卡片标记。

        card_type 决定 stream_callback 如何渲染：
        - add     → 参数表格（标题列置顶）
        - update  → 参数表格（标题列置顶）
        - delete  → 删除条目 ID 表格
        - list    → 实际记忆条目表格（查库）
        - search  → 搜索匹配条目表格（查库）
        """
        try:
            args = json.loads(tool_input) if tool_input else {}
        except json.JSONDecodeError:
            args = {}

        short = tool_name.replace("mcp__memory__", "")
        # MemoryAddProj → add_proj → card_type="add", scope="proj"
        # MemoryListUser → list_user → card_type="list", scope="user"
        parts = re.split(r"(?=[A-Z])", short.replace("Memory", ""))
        card_type_map = {
            "Add": "add", "Delete": "delete",
            "Update": "update", "List": "list", "Search": "search",
        }
        card_type = None
        scope = ""
        for p in parts:
            if p in card_type_map:
                card_type = card_type_map[p]
            else:
                scope = p.lower()   # Proj → proj, User → user

        # ── 根据操作类型查库获取真实 entries ───────────────────────────────
        entries = []

        if card_type in ("list", "search"):
            # list/search — 查询实际条目（project_path 缺失时使用 default_project_path）
            query = args.get("query", "")
            project_path = args.get("project_path", "") or default_project_path
            user_open_id = args.get("user_open_id", "")

            if memory_manager is not None:
                try:
                    if scope == "proj":
                        if card_type == "list":
                            mems = memory_manager.get_project_memories(project_path)
                            entries = [{"id": m.id, "title": m.title,
                                        "content": m.content, "keywords": m.keywords} for m in mems]
                        elif card_type == "search" and query:
                            results = memory_manager.search_project_memories(query, project_path)
                            entries = [{"id": r.memory.id, "title": r.memory.title,
                                        "content": r.memory.content,
                                        "keywords": r.memory.keywords} for r in results]
                        else:
                            entries = []
                    else:
                        # user scope — mirror memory_tools.py fallback: empty user_open_id → get all
                        if user_open_id:
                            prefs = memory_manager.get_preferences_by_user(user_open_id)
                            if card_type == "search" and query:
                                prefs = memory_manager.search_preferences(query, user_open_id)
                        else:
                            prefs = memory_manager.get_all_preferences()
                        entries = [{"id": p.id, "title": p.title,
                                    "content": p.content, "keywords": p.keywords} for p in prefs]
                except Exception:
                    entries = []

        elif card_type == "delete":
            # delete — 直接用 tool_input 里的 id（CC 先删 DB，无法再查）
            mem_id = args.get("id", "")
            if mem_id:
                entries = [{"id": mem_id}]

        # add / update — entries 就是当前操作的参数
        if not entries and card_type in ("add", "update"):
            entries = [{"title": args.get("title", ""),
                        "content": args.get("content", ""),
                        "keywords": args.get("keywords", ""),
                        "id": args.get("id", "") or "(新增)"}]

        return _MemoryCardMarker(tool_name, card_type, entries, tool_input)

    def _format_bash_tool(self, tool_input: str) -> str:
        """Format Bash tool call as a markdown code block.

        If description exists, append it to the header line.
        Code block only contains the command.
        """
        try:
            data = json.loads(tool_input)
        except json.JSONDecodeError:
            # 不是合法 JSON，降级
            return f"💻 **Bash**\n```bash\n{tool_input}\n```"

        command = data.get("command", tool_input)
        description = data.get("description")

        icon = self.tool_icons.get("Bash", "💻")
        if description:
            header = f"{icon} **Bash** — {description}"
        else:
            header = f"{icon} **Bash**"

        return f"{header}\n```bash\n{command}\n```"

    def _format_read_tool(self, tool_input: str) -> str:
        """Format Read tool call with backtick-wrapped file path and optional offset/limit."""
        try:
            data = json.loads(tool_input)
            file_path = data.get("file_path", tool_input)
        except json.JSONDecodeError:
            file_path = tool_input
            data = {}

        icon = self.tool_icons.get("Read", "📖")
        extras = []
        if data.get("offset") is not None:
            extras.append(f"offset {data['offset']}")
        if data.get("limit") is not None:
            extras.append(f"limit {data['limit']}")

        if extras:
            title = f"**Read** — " + " — ".join(extras)
            path_line = f"`{file_path}`"
        else:
            title = f"**Read**"
            path_line = f"`{file_path}`"
        return f"{icon} {title}\n{path_line}"

    def _format_todowrite_tool(self, tool_input: str) -> str:
        """Format TodoWrite tool call as a markdown table."""
        try:
            data = json.loads(tool_input)
            todos = data.get("todos", [])
        except json.JSONDecodeError:
            todos = []

        if not isinstance(todos, list):
            todos = []

        if not todos:
            return "✅ 所有任务已完成！"

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        rows = ["| 状态 | 待办事项 | 当前动作 |", "|------|----------|----------|"]
        for t in todos:
            icon = status_icon.get(t.get("status", "pending"), "⬜")
            content = str(t.get("content", "")).replace("\n", " ").replace("|", "\\|")
            active = str(t.get("activeForm", "")).replace("\n", " ").replace("|", "\\|")
            rows.append(f"| {icon} | {content} | {active} |")

        return "📋 Todo List\n\n" + "\n".join(rows)

    def should_use_card(self, text: str) -> bool:
        """Decide whether to send as Feishu Interactive Card vs post.

        Delegates to the module-level should_use_card for the actual logic.
        """
        return should_use_card(text)

    def split_messages(self, text: str) -> list[str]:
        """Split long text into chunks under Feishu's limit."""
        if len(text) <= FEISHU_MAX_MESSAGE_LENGTH:
            return [text] if text else []

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 <= FEISHU_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    chunks.append(current.rstrip())
                # If single line exceeds limit, split by chars
                if len(line) > FEISHU_MAX_MESSAGE_LENGTH:
                    while len(line) > FEISHU_MAX_MESSAGE_LENGTH:
                        chunks.append(line[:FEISHU_MAX_MESSAGE_LENGTH])
                        line = line[FEISHU_MAX_MESSAGE_LENGTH:]
                    current = line + "\n"
                else:
                    current = line + "\n"

        if current.strip():
            chunks.append(current.rstrip())

        return [c for c in chunks if c.strip()]
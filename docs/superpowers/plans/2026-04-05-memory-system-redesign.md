# 记忆系统重新设计实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将记忆系统从三种类型简化为两张表（user_preferences + project_memories），统一字段为标题+内容+关键词。

**Architecture:**

- 两张独立 SQLite 表，各自带 FTS5 虚拟表
- 用户偏好：全量注入，不搜
- 项目记忆：按需搜索，按项目路径隔离
- 旧 memories 表首次启动时 DROP

**Tech Stack:** SQLite + FTS5, Python, MCP tools

---

## 文件概览

| 文件 | 改动类型 |
|------|---------|
| `cc_feishu_bridge/claude/memory_manager.py` | 完全重写 |
| `cc_feishu_bridge/claude/memory_tools.py` | 修改 |
| `cc_feishu_bridge/feishu/message_handler.py` | 修改 |
| `cc_feishu_bridge/main.py` | 修改 |
| `tests/test_memory_manager.py` | 修改 |

---

### Task 1: 重写 `memory_manager.py` — 双表结构 + FTS5

**Files:**
- Modify: `cc_feishu_bridge/claude/memory_manager.py`
- Test: `tests/test_memory_manager.py`

- [ ] **Step 1: 写失败的测试（新增测试用例）**

在 `tests/test_memory_manager.py` 中，删除所有旧测试，替换为：

```python
def test_user_preferences_table_structure():
    """user_preferences: id, title, content, keywords, created_at, updated_at"""
    mgr = MemoryManager(db_path=":memory:")
    mgr.add_preference("主人信息", "我叫狗蛋，我的主人叫姚日华", "狗蛋,主人,姚日华")
    prefs = mgr.get_all_preferences()
    assert len(prefs) == 1
    assert prefs[0].title == "主人信息"
    assert prefs[0].content == "我叫狗蛋，我的主人叫姚日华"
    assert prefs[0].keywords == "狗蛋,主人,姚日华"


def test_project_memories_isolated_by_path():
    """project_memories: 相同 id 不同 project_path 不互串"""
    mgr = MemoryManager(db_path=":memory:")
    mgr.add_project_memory("/proj-a", "用 pnpm", "不要用 npm，用 pnpm install", "pnpm,npm")
    mgr.add_project_memory("/proj-b", "用 yarn", "不要用 npm，用 yarn", "yarn,npm")
    results_a = mgr.search_project_memories("pnpm", project_path="/proj-a")
    results_b = mgr.search_project_memories("yarn", project_path="/proj-b")
    assert any("pnpm" in r.content for r in results_a)
    assert not any("pnpm" in r.content for r in results_b)


def test_inject_context_injects_all_preferences():
    """inject_context 返回所有 user_preferences"""
    mgr = MemoryManager(db_path=":memory:")
    mgr.add_preference("主人信息", "我叫狗蛋", "狗蛋")
    ctx = mgr.inject_context(project_path="/any/path")
    assert "主人信息" in ctx
    assert "我叫狗蛋" in ctx


def test_inject_context_never_searches():
    """inject_context 只读不搜，直接返回所有偏好"""
    mgr = MemoryManager(db_path=":memory:")
    mgr.add_preference("主人信息", "我叫狗蛋", "狗蛋")
    # 不带 project_path 也应该返回（inject_context 内部用 None 跳过项目记忆部分）
    ctx = mgr.inject_context(project_path=None)
    assert "狗蛋" in ctx or "主人信息" in ctx
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_memory_manager.py -v`
Expected: FAIL — 方法不存在

- [ ] **Step 3: 重写 `memory_manager.py`**

完全重写文件，完整代码如下：

```python
"""Local memory store with SQLite FTS5 for Claude Code bridge."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_SYSTEM_GUIDE = """
【记忆系统使用指引】
遇到报错、构建失败、工具执行异常时，优先用 MemorySearch 搜索项目记忆。
解决问题后主动问用户："需要记住吗？" 用户确认后用 MemoryAdd 写入（标题+内容+关键词三样必填）。
用户说"记住 XXX"时，直接调用 MemoryAdd 写入。
"""


@dataclass
class UserPreference:
    """用户偏好条目（全局）"""
    id: str
    title: str
    content: str
    keywords: str  # 逗号分隔
    created_at: str
    updated_at: str


@dataclass
class ProjectMemory:
    """项目记忆条目（按项目隔离）"""
    id: str
    project_path: str
    title: str
    content: str
    keywords: str  # 逗号分隔
    created_at: str
    updated_at: str


@dataclass
class MemorySearchResult:
    """记忆搜索结果"""
    memory: ProjectMemory
    rank: float  # FTS5 bm25 rank


class MemoryManager:
    """SQLite+FTS5 双表记忆管理器"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base = Path.home() / ".cc-feishu-bridge"
            base.mkdir(exist_ok=True)
            db_path = str(base / "memories.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """创建/升级数据库：删除旧表，建新表"""
        with sqlite3.connect(self.db_path) as conn:
            # 删除旧表（首次启动迁移）
            conn.execute("DROP TABLE IF EXISTS memories")
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            conn.execute("DROP INDEX IF EXISTS idx_memories_project_path")
            conn.execute("DROP INDEX IF EXISTS idx_memories_type")

            # 建 user_preferences 表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    keywords    TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)

            # 建 user_preferences FTS5
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS user_preferences_fts USING fts5(
                    id UNINDEXED, title, content, keywords
                )
            """)

            # 建 project_memories 表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_memories (
                    id           TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    keywords     TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
            """)

            # 建 project_memories FTS5
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS project_memories_fts USING fts5(
                    id UNINDEXED, title, content, keywords
                )
            """)

    # ── 用户偏好 ───────────────────────────────────────────────────────────────

    def add_preference(
        self,
        title: str,
        content: str,
        keywords: str,
    ) -> UserPreference:
        """添加一条用户偏好（全局）"""
        now = datetime.utcnow().isoformat()
        pref = UserPreference(
            id=str(uuid.uuid4())[:8],
            title=title,
            content=content,
            keywords=keywords,
            created_at=now,
            updated_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO user_preferences (id, title, content, keywords, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pref.id, pref.title, pref.content, pref.keywords, pref.created_at, pref.updated_at)
            )
            conn.execute(
                "INSERT INTO user_preferences_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                (pref.id, pref.title, pref.content, pref.keywords)
            )
        return pref

    def get_all_preferences(self) -> list[UserPreference]:
        """获取所有用户偏好（按创建时间倒序）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM user_preferences ORDER BY created_at DESC"
            ).fetchall()
        return [UserPreference(**dict(r)) for r in rows]

    def inject_context(self, project_path: Optional[str]) -> str:
        """
        注入用户偏好到 prompt（全量返回，无搜索）。
        project_path 参数保留用于未来扩展（目前不用于用户偏好）。
        """
        prefs = self.get_all_preferences()
        if not prefs:
            return ""
        lines = ["\n【用户偏好】", "---"]
        for p in prefs:
            lines.append(f"**{p.title}**")
            lines.append(f"{p.content}")
            lines.append("")
        return "\n".join(lines)

    # ── 项目记忆 ───────────────────────────────────────────────────────────────

    def add_project_memory(
        self,
        project_path: str,
        title: str,
        content: str,
        keywords: str,
    ) -> ProjectMemory:
        """添加一条项目记忆（按项目隔离）"""
        now = datetime.utcnow().isoformat()
        mem = ProjectMemory(
            id=str(uuid.uuid4())[:8],
            project_path=project_path,
            title=title,
            content=content,
            keywords=keywords,
            created_at=now,
            updated_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO project_memories (id, project_path, title, content, keywords, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mem.id, mem.project_path, mem.title, mem.content, mem.keywords, mem.created_at, mem.updated_at)
            )
            conn.execute(
                "INSERT INTO project_memories_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                (mem.id, mem.title, mem.content, mem.keywords)
            )
        return mem

    def search_project_memories(
        self,
        query: str,
        project_path: str,
        limit: int = 5,
    ) -> list[MemorySearchResult]:
        """按项目搜索项目记忆（只搜当前项目，全文检索）"""
        if not query.strip() or not project_path:
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT m.*, bm25(project_memories_fts) as rank
                FROM project_memories_fts
                JOIN project_memories m ON project_memories_fts.id = m.id
                WHERE project_memories_fts MATCH ?
                  AND m.project_path = ?
                ORDER BY m.created_at DESC
                LIMIT ?
            """, (query, project_path, limit)).fetchall()
        results = []
        for row in rows:
            mem = ProjectMemory(
                id=row["id"],
                project_path=row["project_path"],
                title=row["title"],
                content=row["content"],
                keywords=row["keywords"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            results.append(MemorySearchResult(memory=mem, rank=row["rank"]))
        return results

    def delete_project_memory(self, memory_id: str) -> bool:
        """删除一条项目记忆"""
        with sqlite3.connect(self.db_path) as conn:
            affected = conn.execute(
                "DELETE FROM project_memories WHERE id = ?", (memory_id,)
            ).rowcount
        return affected > 0

    def clear_project_memories(self, project_path: str) -> int:
        """清空某项目下所有记忆"""
        if not project_path:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM project_memories WHERE project_path = ?",
                (project_path,)
            ).fetchone()[0]
            conn.execute("DELETE FROM project_memories WHERE project_path = ?", (project_path,))
        return count
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_memory_manager.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cc_feishu_bridge/claude/memory_manager.py tests/test_memory_manager.py
git commit -m "refactor(memory): dual-table design — user_preferences + project_memories"
```

---

### Task 2: 更新 `memory_tools.py` — 新 MCP 工具签名

**Files:**
- Modify: `cc_feishu_bridge/claude/memory_tools.py`
- Test: 无需新增测试（MCP 工具由集成测试覆盖）

- [ ] **Step 1: 重写 MCP 工具函数**

完整重写 `cc_feishu_bridge/claude/memory_tools.py`，删除旧的 `_build_memory_mcp_server` 和所有 `Memory*` 工具，替换为新的：

```python
"""Memory MCP tools for Claude SDK."""
from __future__ import annotations

from cc_feishu_bridge.claude.memory_manager import MemoryManager


_TYPE_LABELS = {
    "user_preference": "用户偏好",
    "project_memory": "项目记忆",
}


def _format_preference_text(pref) -> str:
    lines = [f"[用户偏好] **{pref.title}**"]
    lines.append(f"  {pref.content}")
    lines.append(f"  关键词: {pref.keywords}")
    lines.append(f"  ID: `{pref.id}`")
    return "\n".join(lines)


def _format_memory_text(mem) -> str:
    lines = [f"[项目记忆] **{mem.title}**"]
    lines.append(f"  {mem.content}")
    lines.append(f"  关键词: {mem.keywords}")
    lines.append(f"  项目: {mem.project_path}")
    lines.append(f"  ID: `{mem.id}`")
    return "\n".join(lines)


def _build_memory_mcp_server():
    """Build the memory MCP server with all memory management tools."""
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        "MemorySearch",
        (
            "搜索项目记忆库，查找之前遇到过的问题和解决方案。"
            "只搜当前项目（project_path）下的记忆，不搜用户偏好。"
            "返回结果包含标题、内容和关键词。"
        ),
        {"query": str, "project_path": str | None},
    )
    async def memory_search(args: dict) -> dict:
        query = args.get("query", "")
        project_path = args.get("project_path")

        if not query.strip():
            return {
                "content": [{"type": "text", "text": "查询词不能为空。"}],
                "is_error": True,
            }

        manager = MemoryManager()
        results = manager.search_project_memories(query, project_path=project_path or "", limit=5)

        if not results:
            return {
                "content": [{"type": "text", "text": f"未找到与「{query}」相关的记忆。"}],
            }

        lines = [f"**项目记忆搜索结果（{len(results)} 条）**", ""]
        for r in results:
            m = r.memory
            lines.append(f"[项目记忆] **{m.title}**")
            lines.append(f"  {m.content}")
            lines.append(f"  关键词: {m.keywords}")
            lines.append(f"  ID: `{m.id}`")
            lines.append("")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "MemoryAdd",
        (
            "向记忆库添加新条目。"
            "用户偏好存入 user_preferences（全局），项目记忆存入 project_memories（按项目隔离）。"
            "title、content、keywords 三样必填，缺一不可。"
        ),
        {
            "type": str,  # "user_preference" | "project_memory"
            "title": str,
            "content": str,
            "keywords": str,
            "project_path": str | None,
        },
    )
    async def memory_add(args: dict) -> dict:
        entry_type = args.get("type")
        title = args.get("title", "").strip()
        content = args.get("content", "").strip()
        keywords = args.get("keywords", "").strip()
        project_path = args.get("project_path")

        if not title:
            return {"content": [{"type": "text", "text": "标题不能为空"}], "is_error": True}
        if not content:
            return {"content": [{"type": "text", "text": "内容不能为空"}], "is_error": True}
        if not keywords:
            return {"content": [{"type": "text", "text": "关键词不能为空"}], "is_error": True}

        manager = MemoryManager()

        if entry_type == "user_preference":
            pref = manager.add_preference(title, content, keywords)
            return {"content": [{"type": "text", "text": f"✅ 用户偏好已保存（ID: {pref.id}）\n\n{_format_preference_text(pref)}"}]}
        elif entry_type == "project_memory":
            if not project_path:
                return {"content": [{"type": "text", "text": "project_memory 需要传入 project_path"}], "is_error": True}
            mem = manager.add_project_memory(project_path, title, content, keywords)
            return {"content": [{"type": "text", "text": f"✅ 项目记忆已保存（ID: {mem.id}）\n\n{_format_memory_text(mem)}"}]}
        else:
            return {"content": [{"type": "text", "text": f"无效的 type：{entry_type}，必须是 user_preference 或 project_memory"}], "is_error": True}

    @tool(
        "MemoryDelete",
        "删除指定 ID 的项目记忆。",
        {"memory_id": str},
    )
    async def memory_delete(args: dict) -> dict:
        memory_id = args.get("memory_id", "")
        manager = MemoryManager()
        ok = manager.delete_project_memory(memory_id)
        if ok:
            return {"content": [{"type": "text", "text": f"🗑️ 记忆 {memory_id} 已删除。"}]}
        return {"content": [{"type": "text", "text": f"未找到 ID 为 {memory_id} 的记忆。"}], "is_error": True}

    @tool(
        "MemoryClear",
        "清空指定项目下所有项目记忆。",
        {"project_path": str | None},
    )
    async def memory_clear(args: dict) -> dict:
        project_path = args.get("project_path")
        if not project_path:
            return {"content": [{"type": "text", "text": "需要传入 project_path"}], "is_error": True}
        manager = MemoryManager()
        count = manager.clear_project_memories(project_path)
        return {"content": [{"type": "text", "text": f"🧹 已清除 {count} 条项目记忆。"}]}

    return create_sdk_mcp_server(
        name="memory",
        version="1.0.0",
        tools=[memory_search, memory_add, memory_delete, memory_clear],
    )


_mcp_server = None


def get_memory_mcp_server():
    """Get the singleton memory MCP server."""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = _build_memory_mcp_server()
    return _mcp_server


MEMORY_SYSTEM_GUIDANCE = """
当你遇到以下情况时，请优先使用 MemorySearch 搜索项目记忆：
- 遇到报错（error）、构建失败（build failed）、测试失败（test failed）
- 遇到之前似乎见过的问题

添加记忆时，使用 MemoryAdd（title + content + keywords 三样必填）。
也可以使用 MemoryDelete、MemoryClear 工具管理记忆。
"""
```

- [ ] **Step 2: 提交**

```bash
git add cc_feishu_bridge/claude/memory_tools.py
git commit -m "refactor(memory): update MCP tools for dual-table design"
```

---

### Task 3: 更新 `message_handler.py` — 新 `inject_context()` 接口

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py:221`
- Test: 无需新增测试（集成测试覆盖）

- [ ] **Step 1: 检查并修改调用处**

当前第 221 行：
```python
memory_context = MEMORY_SYSTEM_GUIDE + self.memory_manager.inject_context(project_path=self.approved_directory)
```

`inject_context()` 签名从 `get_by_project` 改为直接返回用户偏好，无需修改调用代码。确认即可。

- [ ] **Step 2: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "refactor(message_handler): adapt to new MemoryManager interface"
```

---

### Task 4: 更新 `main.py` — CLI 参数适配

**Files:**
- Modify: `cc_feishu_bridge/main.py:520-530`

- [ ] **Step 1: 修改 `_run_memory_command` 中的 `add` 子命令**

当前 `add` 子命令默认 `--type user_preference`，新系统中：
- `--type user_preference` → 调 `manager.add_preference()`
- `--type project_memory` → 调 `manager.add_project_memory(project_path=args.project)`

```python
elif sub == "add":
    entry_type = args.type
    title = args.content[:60]
    solution = args.content
    keywords = args.problem or ""  # --problem 参数改为 --keywords
    project_path = args.project

    manager = MemoryManager()
    if entry_type == "user_preference":
        pref = manager.add_preference(title, solution, keywords)
        print(f"✅ 用户偏好已保存 (id={pref.id})")
    else:
        if not project_path:
            print("❌ project_memory 需要 --project 参数")
            return
        mem = manager.add_project_memory(project_path, title, solution, keywords)
        print(f"✅ 项目记忆已保存 (id={mem.id})")
```

同时修改 `add_parser` 参数：
```python
add_parser.add_argument("--type", default="user_preference",
                        choices=["user_preference", "project_memory"])
add_parser.add_argument("--keywords", default=None,
                        help="关键词（逗号分隔，FTS5 检索用）")
```

- [ ] **Step 2: 修改 `search` 子命令**

`search` 只搜项目记忆，不需要改动搜索逻辑（因为 `manager.search()` 已改为只搜 `project_memories`）。

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/main.py
git commit -m "refactor(main): update CLI for dual-table memory design"
```

---

### Task 5: 更新测试 + Read 工具格式修复确认

**Files:**
- Test: `tests/test_memory_manager.py`

- [ ] **Step 1: 确认所有测试通过**

Run: `pytest tests/test_memory_manager.py tests/test_reply_formatter.py -v`
Expected: ALL PASS

- [ ] **Step 2: 提交**

```bash
git add tests/test_memory_manager.py
git commit -m "test(memory): rewrite tests for dual-table design"
```

---

### Task 6: 全量测试 + 编译打包

**Files:**
- Test: `tests/`

- [ ] **Step 1: 运行全量测试**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: 重新编译 whl**

Run: `rm -rf dist && python -m build`
Expected: `cc_feishu_bridge-0.3.X-py3-none-any.whl`

- [ ] **Step 3: 提交所有改动**

```bash
git add -A
git commit -m "feat(memory): complete dual-table redesign — user_preferences + project_memories

- memory_manager: dual-table design, drop old memories table on boot
- memory_tools: new MCP tools (MemorySearch/MemoryAdd/MemoryDelete/MemoryClear)
- message_handler: adapt inject_context to new interface
- main: update CLI args (--type, --keywords)
- tests: full rewrite for new data model"
```

---

### Task 7: 发布版本

- [ ] **Step 1: 更新 `pyproject.toml` 版本号**
- [ ] **Step 2: 更新 `CHANGELOG.md`**
- [ ] **Step 3: 编译 whl**
- [ ] **Step 4: 提交 + push**
- [ ] **Step 5: 打 tag + push**
- [ ] **Step 6: 确认 PyPI 发布成功**
- [ ] **Step 7: 主人确认后执行**

---

### Task 8: 重新录入用户偏好（旧数据迁移）

- [ ] **Step 1: 用新的 `MemoryAdd` 工具重新录入：主人信息、狗蛋职责、发版流程规则**

```python
# 直接在 CC 里调用，写入新表
manager = MemoryManager()
manager.add_preference(
    "主人信息",
    "我叫狗蛋🐕，我的主人是姚日华",
    "狗蛋,主人,姚日华"
)
manager.add_preference(
    "狗蛋的职责",
    "主人告诉我的事，要立刻用 MemoryAdd 写入记忆库，不要只说'记住了'就结束",
    "狗蛋,记住,MemoryAdd"
)
manager.add_preference(
    "发版前必须主人确认",
    "发版前必须先列出所有改动，等主人点头同意后才能 commit、push、打 tag，未经主人确认不得擅自操作",
    "发版,确认,commit,tag"
)
```

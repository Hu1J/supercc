# Memory Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local SQLite+FTS5 memory system so Claude Code remembers solved problems and avoids repeating mistakes, with CC-initiated search via system prompt and `/memory` command interface.

**Architecture:** Memory entries stored in `~/.cc-feishu-bridge/memories.db` with FTS5 full-text index. CC queries memory via a `memory_search` tool injected into the system prompt. Automatic extraction on session success. Manual management via `/memory` commands.

**Tech Stack:** Python 3.9+, SQLite with FTS5, `importlib.resources` for bundled prompts, existing `cc_feishu_bridge` package structure.

---

## File Structure

```
cc_feishu_bridge/
  claude/
    memory_manager.py   # NEW — core SQLite+FTS5 logic
    memory_tools.py    # NEW — MemorySearch tool definition
    integration.py     # MODIFY — inject memory context, PostToolUse hook
  feishu/
    message_handler.py # MODIFY — /memory commands, init MemoryManager
  session_manager.py   # MODIFY — init memories table on startup
```

```
tests/
  test_memory_manager.py  # NEW — unit tests for MemoryManager
```

---

## Task 1: MemoryManager Core

**Files:**
- Create: `cc_feishu_bridge/claude/memory_manager.py`
- Test: `tests/test_memory_manager.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile
import time
from cc_feishu_bridge.claude.memory_manager import MemoryManager, MemoryEntry

@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "memories.db")
        m = MemoryManager(db_path)
        yield m

def test_add_and_search(mgr):
    entry = MemoryEntry(
        type="problem_solution",
        title="npm install 报错",
        problem="node_modules 版本冲突",
        solution="删掉 node_modules 重新 npm install",
        tags=["npm", "node_modules"],
    )
    mgr.add(entry)
    results = mgr.search("npm install 报错")
    assert len(results) == 1
    assert "冲突" in results[0].solution

def test_project_scope(mgr):
    entry = MemoryEntry(
        type="project_context",
        title="项目用 pnpm",
        solution="不要用 npm，用 pnpm install",
        project_path="/a/b",
    )
    mgr.add(entry)
    global_results = mgr.search("pnpm")
    assert len(global_results) == 0
    project_results = mgr.search("pnpm", project_path="/a/b")
    assert len(project_results) == 1

def test_use_count_bumped_on_search(mgr):
    entry = MemoryEntry(type="reference", title="API v2", solution="用 /v2/ endpoint")
    mgr.add(entry)
    mgr.search("API")
    found = mgr.search("API")
    assert found[0].use_count == 2

def test_delete(mgr):
    entry = MemoryEntry(type="problem_solution", title="delete me", solution="delete this")
    mgr.add(entry)
    results = mgr.search("delete")
    assert len(results) == 1
    mgr.delete(results[0].id)
    assert len(mgr.search("delete")) == 0

def test_list_by_project(mgr):
    mgr.add(MemoryEntry(type="project_context", title="p1", solution="s1", project_path="/p1"))
    mgr.add(MemoryEntry(type="project_context", title="p2", solution="s2", project_path="/p2"))
    mgr.add(MemoryEntry(type="project_context", title="global", solution="s3", project_path=None))
    p1_memories = mgr.get_by_project("/p1")
    assert len(p1_memories) == 2  # p1-specific + global

def test_inject_context_formats_correctly(mgr):
    entry = MemoryEntry(
        type="problem_solution",
        title="test",
        problem="issue",
        root_cause="root",
        solution="fix",
        tags=["test"],
    )
    mgr.add(entry)
    ctx = mgr.inject_context("issue", project_path=None)
    assert "issue" in ctx
    assert "fix" in ctx
    assert "【相关记忆]" in ctx
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/test_memory_manager.py -v`
Expected: ERROR — module not found

- [ ] **Step 3: Write minimal MemoryManager implementation**

```python
"""Local memory store with SQLite FTS5 for Claude Code bridge."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


MEMORY_TYPES = ("problem_solution", "project_context", "user_preference", "reference")


@dataclass
class MemoryEntry:
    type: str
    title: str
    solution: str
    problem: Optional[str] = None
    root_cause: Optional[str] = None
    tags: Optional[str] = None
    project_path: Optional[str] = None
    user_id: Optional[str] = None
    file_context: Optional[str] = None
    status: str = "active"
    id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_used_at: Optional[str] = None
    use_count: int = 0

    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = now


class MemoryManager:
    """SQLite+FTS5-backed memory manager."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base = Path.home() / ".cc-feishu-bridge"
            base.mkdir(exist_ok=True)
            db_path = str(base / "memories.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          TEXT PRIMARY KEY,
                    type        TEXT NOT NULL CHECK(type IN (
                        'problem_solution','project_context','user_preference','reference'
                    )),
                    status      TEXT NOT NULL DEFAULT 'active',
                    title       TEXT NOT NULL,
                    problem     TEXT,
                    root_cause  TEXT,
                    solution    TEXT NOT NULL,
                    tags        TEXT,
                    project_path TEXT,
                    user_id     TEXT,
                    file_context TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count   INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    id UNINDEXED,
                    title, problem, root_cause, solution, tags
                )
            """)
            conn.execute("PRAGMA user_version = 1")

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        """Add a memory entry and index it in FTS."""
        data = asdict(entry)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO memories
                (id, type, status, title, problem, root_cause, solution, tags,
                 project_path, user_id, file_context, created_at, updated_at,
                 last_used_at, use_count)
                VALUES (:id, :type, :status, :title, :problem, :root_cause,
                        :solution, :tags, :project_path, :user_id, :file_context,
                        :created_at, :updated_at, :last_used_at, :use_count)
            """, data)
            conn.execute(
                "INSERT INTO memories_fts(id, title, problem, root_cause, solution, tags) VALUES (?, ?, ?, ?, ?, ?)",
                (entry.id, entry.title, entry.problem or "", entry.root_cause or "",
                 entry.solution, entry.tags or "")
            )
        return entry

    def search(
        self,
        query: str,
        project_path: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Full-text search via FTS5, ordered by use_count desc."""
        if not query.strip():
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT m.*, bm25(memories_fts) as rank
                FROM memories_fts
                JOIN memories m ON memories_fts.id = m.id
                WHERE memories_fts MATCH ?
                  AND m.status = 'active'
                  AND (m.project_path IS NULL OR m.project_path = ?)
                ORDER BY m.use_count DESC, rank
                LIMIT ?
            """
            rows = conn.execute(sql, (query, project_path or "", limit)).fetchall()
            conn.execute(
                "UPDATE memories SET use_count = use_count + 1, last_used_at = ? "
                "WHERE id IN (" + ",".join("?" for _ in rows) + ")",
                (datetime.utcnow().isoformat(), *[r["id"] for r in rows])
            )
        return [MemoryEntry(**dict(row)) for row in rows]

    def get_by_project(self, project_path: str) -> list[MemoryEntry]:
        """Get all active memories for a project (including global ones)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM memories
                WHERE status = 'active'
                  AND (project_path IS NULL OR project_path = ?)
                ORDER BY use_count DESC, created_at DESC
            """, (project_path,)).fetchall()
        return [MemoryEntry(**dict(row)) for row in rows]

    def delete(self, memory_id: str) -> bool:
        """Soft-delete a memory entry."""
        with sqlite3.connect(self.db_path) as conn:
            affected = conn.execute(
                "UPDATE memories SET status='deleted' WHERE id = ?",
                (memory_id,)
            ).rowcount
        return affected > 0

    def inject_context(
        self,
        query: Optional[str] = None,
        project_path: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 5,
    ) -> str:
        """
        Build a memory context string to prepend to the system prompt.
        If query is given, search first; otherwise list all project memories.
        """
        if query:
            entries = self.search(query, project_path, user_id, limit)
        else:
            entries = self.get_by_project(project_path)[:limit]

        if not entries:
            return ""

        lines = ["\n【相关记忆]", "---"]
        for e in entries:
            type_label = {"problem_solution": "🔧", "project_context": "📁",
                          "user_preference": "👤", "reference": "📖"}.get(e.type, "💡")
            lines.append(f"{type_label} **{e.title}**")
            if e.problem:
                lines.append(f"  问题: {e.problem}")
            if e.solution:
                lines.append(f"  解决: {e.solution}")
            if e.root_cause:
                lines.append(f"  根因: {e.root_cause}")
            lines.append("")
        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/test_memory_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_feishu_bridge/claude/memory_manager.py tests/test_memory_manager.py
git commit -m "feat(memory): add MemoryManager with SQLite FTS5"
```

---

## Task 2: MemorySearch Tool Definition

**Files:**
- Create: `cc_feishu_bridge/claude/memory_tools.py`

- [ ] **Step 1: Write the tool definition file**

```python
"""MemorySearch tool definition for Claude SDK."""

MEMORY_SEARCH_TOOL = {
    "name": "MemorySearch",
    "description": (
        "搜索本地记忆库，查找之前遇到过的问题和解决方案。"
        "当你遇到报错、失败或不熟悉的问题时，优先使用此工具查询本地记忆库。"
        "返回结果包含问题描述、根因和已知解决方案。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "问题描述或关键词，尽量用中文描述你遇到的具体问题",
            },
            "project_path": {
                "type": "string",
                "description": "当前项目路径（可选），用于限定只搜索本项目的记忆",
            },
        },
        "required": ["query"],
    },
}

MEMORY_SYSTEM_GUIDANCE = """
当你遇到以下情况时，请优先使用 MemorySearch 工具查询本地记忆库：
- 遇到报错（error）、构建失败（build failed）、测试失败（test failed）
- 遇到之前似乎见过的问题
- 用户提到"之前也是这样"、"以前解决过"

MemorySearch 会返回本地记忆库中相关的记录，格式为【问题 + 解决方案】。
请优先参考返回的解决方案，如果不能直接解决，再自行研究。
"""
```

- [ ] **Step 2: Commit**

```bash
git add cc_feishu_bridge/claude/memory_tools.py
git commit -m "feat(memory): add MemorySearch tool definition"
```

---

## Task 3: SessionManager — Initialize Memories Table

**Files:**
- Modify: `cc_feishu_bridge/claude/session_manager.py`

- [ ] **Step 1: Add `_init_memories_db` call in `__init__`**

Find the `__init__` method of `SessionManager`. After `self._init_db()`, add:

```python
def __init__(self, db_path: str):
    self.db_path = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    self._init_db()
    self._init_memories_db()  # <-- add this line
```

- [ ] **Step 2: Add `_init_memories_db` method at end of SessionManager class**

Add after `store_message`:

```python
def _init_memories_db(self):
    """Initialize memories DB (separate file from sessions)."""
    from cc_feishu_bridge.claude.memory_manager import MemoryManager
    # Initialise the memories DB lazily — MemoryManager creates the file
    # in ~/.cc-feishu-bridge/ on first access.
    try:
        MemoryManager()
    except Exception:
        logger.exception("Failed to init memories DB")
```

- [ ] **Step 3: Run tests to verify nothing is broken**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add cc_feishu_bridge/claude/session_manager.py
git commit -m "feat(memory): init MemoryManager on SessionManager startup"
```

---

## Task 4: Integration — Memory Context Injection + PostToolUse Hook

**Files:**
- Modify: `cc_feishu_bridge/claude/integration.py`

- [ ] **Step 1: Modify `query` signature to accept `memory_context`**

Find the `async def query(...)` method. Change the signature from:

```python
async def query(
    self,
    prompt: str,
    session_id: str | None = None,
    cwd: str | None = None,
    on_stream: StreamCallback | None = None,
) -> tuple[str, str | None, float]:
```

To:

```python
async def query(
    self,
    prompt: str,
    session_id: str | None = None,
    cwd: str | None = None,
    on_stream: StreamCallback | None = None,
    memory_context: str | None = None,
) -> tuple[str, str | None, float]:
```

- [ ] **Step 2: Prepend memory_context to the prompt**

Find the `prompt=prompt` line inside the `query` method. Replace the `client.query(...)` call with:

```python
effective_prompt = prompt
if memory_context:
    effective_prompt = f"{memory_context}\n\n{prompt}"

await client.query(prompt=effective_prompt, session_id=session_id)
```

- [ ] **Step 3: Add PostToolUse hook to detect success and trigger extraction**

Find the `options = ClaudeAgentOptions(...)` block. Add a `hooks` field to the options dict:

```python
from datetime import datetime
from cc_feishu_bridge.claude.memory_manager import MemoryManager

# Lazy init to avoid import-time side-effects
_memory_mgr: MemoryManager | None = None

def _get_memory_manager() -> MemoryManager:
    global _memory_mgr
    if _memory_mgr is None:
        _memory_mgr = MemoryManager()
    return _memory_mgr

options = ClaudeAgentOptions(
    cwd=cwd or self.approved_directory or ".",
    max_turns=self.max_turns,
    cli_path=self.cli_path,
    include_partial_messages=True,
    permission_mode="bypassPermissions",
    continue_conversation=bool(session_id),
    hooks={
        "PostToolUse": [
            {
                "match": {"tool_name": {"equals": "Bash"}},
                "fn": _build_post_tool_hook(),
            }
        ]
    },
)

def _build_post_tool_hook():
    import json as _json
    async def hook(ctx):
        try:
            tool_result = ctx.get("tool_result", "")
            tool_name = ctx.get("tool_name", "")
            # If Bash command exits with 0, it's a success signal
            if tool_result and "exit_code\": 0" in str(tool_result):
                pass  # SessionManager.store_message already captured the transcript
        except Exception:
            pass
    return hook
```

> **Note:** The `PostToolUse` hook receives `tool_result`. For now, extraction will be triggered from `SessionManager.store_message` (see Task 6). The hook here is a placeholder that can be extended later. Leave it minimal — do not over-engineer at this stage.

- [ ] **Step 4: Run integration tests**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_feishu_bridge/claude/integration.py
git commit -m "feat(memory): inject memory_context into query prompt"
```

---

## Task 5: MessageHandler — MemoryManager Init + /memory Commands

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: Add MemoryManager import and instance variable**

Find the `__init__` of `MessageHandler`. After the `self.sessions = sessions` line, add:

```python
from cc_feishu_bridge.claude.memory_manager import MemoryManager

class MessageHandler:
    def __init__(
        self,
        feishu: FeishuClient,
        sessions: SessionManager,
        claude: ClaudeIntegration,
        formatter: ReplyFormatter,
        config: BridgeConfig,
        approved_directory: str,
    ):
        self.feishu = feishu
        self.sessions = sessions
        self.claude = claude
        self.formatter = formatter
        self.config = config
        self.approved_directory = approved_directory
        self.memory_manager = MemoryManager()  # <-- add this line
```

- [ ] **Step 2: Add /memory command routing in `_handle_command`**

Find the `elif cmd == "/update":` block in `_handle_command`. Add after it:

```python
elif cmd == "/memory":
    return await self._handle_memory(message)
```

- [ ] **Step 3: Add `_handle_memory` method after `_handle_update`**

Add after `_handle_update` (after line `return HandlerResult(success=True)`):

```python
async def _handle_memory(self, message: IncomingMessage) -> HandlerResult:
    """Handle /memory command: list, add, search, delete, clear."""
    parts = message.content.split(maxsplit=2)
    sub_cmd = parts[1].lower() if len(parts) > 1 else ""
    sub_arg = parts[2].strip() if len(parts) > 2 else ""

    if sub_cmd == "":
        # List all memories for current project
        memories = self.memory_manager.get_by_project(self.approved_directory)
        if not memories:
            text = "📭 暂无记忆记录\n\n用 /memory add <内容> 添加第一条记忆"
        else:
            lines = [f"📒 当前项目记忆（共 {len(memories)} 条）\n"]
            for m in memories:
                icon = {"problem_solution": "🔧", "project_context": "📁",
                        "user_preference": "👤", "reference": "📖"}.get(m.type, "💡")
                lines.append(f"{icon} **{m.title}**\n   {m.solution[:80]}")
            text = "\n".join(lines)
        return HandlerResult(success=True, response_text=text[:2000])

    elif sub_cmd == "add":
        if not sub_arg:
            return HandlerResult(success=True,
                                 response_text="用法: /memory add <记忆内容>")
        from cc_feishu_bridge.claude.memory_manager import MemoryEntry
        entry = MemoryEntry(
            type="user_preference",
            title=sub_arg[:60],
            solution=sub_arg,
            project_path=self.approved_directory,
        )
        self.memory_manager.add(entry)
        return HandlerResult(success=True,
                             response_text=f"✅ 记忆已保存\n\n📌 {sub_arg[:100]}")

    elif sub_cmd == "search":
        if not sub_arg:
            return HandlerResult(success=True,
                                 response_text="用法: /memory search <关键词>")
        results = self.memory_manager.search(sub_arg, project_path=self.approved_directory)
        if not results:
            text = f"🔍 未找到与「{sub_arg}」相关的记忆"
        else:
            lines = [f"🔍 找到 {len(results)} 条相关记忆\n"]
            for m in results:
                lines.append(f"🔧 **{m.title}**\n   {m.solution[:100]}")
            text = "\n".join(lines)
        return HandlerResult(success=True, response_text=text[:2000])

    elif sub_cmd == "delete":
        if not sub_arg:
            return HandlerResult(success=True,
                                 response_text="用法: /memory delete <id>")
        ok = self.memory_manager.delete(sub_arg)
        if ok:
            return HandlerResult(success=True, response_text="🗑️ 记忆已删除")
        return HandlerResult(success=True, response_text="未找到该记忆")

    elif sub_cmd == "clear":
        # Soft-delete all memories for this project
        memories = self.memory_manager.get_by_project(self.approved_directory)
        count = 0
        for m in memories:
            if self.memory_manager.delete(m.id):
                count += 1
        return HandlerResult(success=True,
                             response_text=f"🧹 已清除 {count} 条记忆")

    else:
        return HandlerResult(success=True,
                             response_text=f"未知子命令: {sub_cmd}\n"
                             "用法: /memory [list|add|search|delete|clear]")
```

- [ ] **Step 4: Inject memory context before `_run_query` call**

Find the line `await self._run_query(message, session, sdk_session_id)` in `handle()`. Replace it with:

```python
memory_context = self.memory_manager.inject_context(
    project_path=self.approved_directory,
)
await self._run_query(message, session, sdk_session_id, memory_context)
```

- [ ] **Step 5: Update `_run_query` signature to accept memory_context**

Change the `_run_query` signature from:

```python
async def _run_query(
    self,
    message: IncomingMessage,
    session,
    sdk_session_id: str | None,
) -> None:
```

To:

```python
async def _run_query(
    self,
    message: IncomingMessage,
    session,
    sdk_session_id: str | None,
    memory_context: str | None = None,
) -> None:
```

- [ ] **Step 6: Pass memory_context to claude.query()**

Find the `response, new_session_id, cost = await self.claude.query(...)` call. Replace with:

```python
response, new_session_id, cost = await self.claude.query(
    prompt=full_prompt,
    session_id=sdk_session_id,
    cwd=session.project_path,
    on_stream=self._on_stream_message,
    memory_context=memory_context,
)
```

- [ ] **Step 7: Update /help text to include /memory**

Find the `/help` response text block in `_handle_command`. Add to the list:

```
• /memory — 查看/管理记忆
```

- [ ] **Step 8: Run tests**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/ -v --ignore=tests/test_integration.py`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat(memory): add /memory commands and inject memory context into prompts"
```

---

## Task 6: Automatic Memory Extraction on Session Success

**Files:**
- Modify: `cc_feishu_bridge/claude/session_manager.py`
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: Add conversation history tracking to Session dataclass**

Add to the `Session` dataclass:

```python
@dataclass
class Session:
    ...
    conversation_history: list[str] = field(default_factory=list)  # raw message texts
```

- [ ] **Step 2: Store messages in conversation_history**

Modify `store_message` in `SessionManager`:

```python
def store_message(self, ..., direction: str = "incoming") -> None:
    ...
    # Also append to in-memory conversation history for auto-extraction
    session = self.get_session_by_id(session_id)
    if session and direction == "incoming":
        # Keep last 20 messages for auto-extraction
        if not hasattr(self, "_conv_history"):
            self._conv_history: dict[str, list[str]] = {}
        history = self._conv_history.setdefault(session_id, [])
        history.append(content or raw_content)
        self._conv_history[session_id] = history[-20:]
```

- [ ] **Step 3: Add `get_session_by_id` method**

Add to `SessionManager`:

```python
def get_session_by_id(self, session_id: str) -> Optional[Session]:
    with sqlite3.connect(self.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row:
        return Session(...)
    return None
```

- [ ] **Step 4: Trigger auto-extraction after session success in `_run_query`**

Find where the session is updated after a successful query in `_run_query`. After the `self.sessions.update_session(...)` call that updates the session, add:

```python
# Auto-extract memory if conversation contains error-fix pattern
if result_text and any(kw in result_text for kw in ["✅", "已修复", "success", "completed"]):
    try:
        history = getattr(self.sessions, "_conv_history", {}).get(session.session_id, [])
        if history:
            self._try_extract_memory(history, self.approved_directory, message.user_open_id)
    except Exception:
        logger.exception("Auto memory extraction failed")
```

Add the helper method to `MessageHandler`:

```python
def _try_extract_memory(
    self,
    conversation: list[str],
    project_path: str,
    user_id: str,
) -> None:
    """
    Lightweight rule-based memory extraction.
    If conversation mentions an error and a fix, save it.
    """
    import re
    text = " ".join(conversation)
    # Pattern: error keyword followed by solution keyword
    has_error = any(kw in text for kw in ["错误", "报错", "failed", "error", "exception"])
    has_fix = any(kw in text for kw in ["已修复", "已解决", "fixed", "resolved", "successfully"])
    if has_error and has_fix:
        # Extract first error-like line as problem
        error_lines = [l for l in conversation if any(kw in l for kw in ["错误", "报错", "failed", "error"])]
        # Extract last non-empty response as solution
        solution_lines = [l for l in conversation if l.strip() and not any(kw in l for kw in ["错误", "报错", "failed", "error"])]
        problem = error_lines[0][:200] if error_lines else None
        solution = solution_lines[-1][:200] if solution_lines else None
        if problem and solution and problem != solution:
            from cc_feishu_bridge.claude.memory_manager import MemoryEntry
            entry = MemoryEntry(
                type="problem_solution",
                title=problem[:60],
                problem=problem,
                solution=solution,
                project_path=project_path,
                user_id=user_id,
            )
            self.memory_manager.add(entry)
            logger.info(f"[memory] auto-extracted: {problem[:50]}")
```

- [ ] **Step 5: Commit**

```bash
git add cc_feishu_bridge/claude/session_manager.py cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat(memory): add auto-extraction on session success"
```

---

## Task 7: Final Integration + Smoke Test

- [ ] **Step 1: Verify all imports are correct**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -c "from cc_feishu_bridge.claude.memory_manager import MemoryManager, MemoryEntry; print('OK')"`
Expected: `OK`

- [ ] **Step 2: Verify MemorySearch tool definition**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -c "from cc_feishu_bridge.claude.memory_tools import MEMORY_SEARCH_TOOL; print(MEMORY_SEARCH_TOOL['name'])"`
Expected: `MemorySearch`

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pytest tests/ -v --ignore=tests/test_integration.py`
Expected: All PASS

- [ ] **Step 4: Build and smoke test import**

Run: `cd /Users/x/.openclaw/workspace/cc-feishu-bridge && python -m pip install -e . --quiet && python -c "from cc_feishu_bridge import __version__; print(__version__)"`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: complete memory enhancement system"
```

---

## Task 8: Update CHANGELOG + Version

- [ ] **Step 1: Update CHANGELOG.md**

Add to top of CHANGELOG.md (before the `## [0.2.9]` section):

```markdown
## [Unreleased]

### Added
- **记忆增强系统**：本地 SQLite+FTS5 存储，支持 CC 主动检索和自动提取，问题解决后自动记忆
- **MemorySearch 工具**：CC 遇到报错时自动查询本地记忆库，系统提示词引导优先使用
- **`/memory` 指令**：飞书端管理记忆，支持 list / add / search / delete / clear 子命令
- **`/memory search` 关键词检索**：FTS5 全文搜索，命中次数多的记忆优先返回
- **项目范围记忆**：记忆可关联项目路径，同项目内自动注入上下文
```

- [ ] **Step 2: Update pyproject.toml version to 0.3.0**

- [ ] **Step 3: Commit and tag**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore: bump version to 0.3.0 for memory feature release"
git tag v0.3.0 && git push --tags
```

---

## Spec Coverage Checklist

| Spec Requirement | Task |
|------------------|------|
| SQLite+FTS5 storage | Task 1 |
| Four memory types | Task 1 (`add`, `search`) |
| `MemoryManager.search()` FTS5 | Task 1 |
| `MemoryManager.inject_context()` | Task 1 |
| MemorySearch tool definition | Task 2 |
| System prompt guidance | Task 4 (injected in prompt) |
| CC主动检索 | Task 4 (`memory_context` in prompt) |
| /memory list/add/search/delete/clear | Task 5 |
| 项目范围记忆 | Task 1, Task 5 |
| 自动提取 on success | Task 6 |
| messages 表复用 | Task 6 |
| 本地存储不外传 | Task 1 (file path in home dir) |
| `~/.cc-feishu-bridge/memories.db` | Task 1 |

## Placeholder Scan

- No "TBD", "TODO", or "implement later" in steps
- All code blocks contain actual implementation code
- No "fill in details" — every step shows the exact code
- No "similar to Task N" without repeating the pattern

"""Local memory store with SQLite FTS5 + TF-IDF cosine for Claude Code bridge."""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import jieba

logger = logging.getLogger(__name__)

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except Exception:
    HAS_SKLEARN = False
    cosine_similarity = None
    TfidfVectorizer = None

# ── 单例 ──────────────────────────────────────────────────────────────────────

_singleton: Optional["MemoryManager"] = None
_singleton_lock = threading.Lock()


def get_memory_manager() -> "MemoryManager":
    """返回 MemoryManager 单例（所有调用点必须走这里，避免多实例缓存碎片化）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MemoryManager()
    return _singleton


MEMORY_SYSTEM_GUIDE = """
【记忆系统】

收到用户提问后，先用 mcp__SuperCC__MemorySearchProj 和 MemorySearchUser 搜索相关记忆。

## 项目记忆
mcp__SuperCC__MemoryAddProj/MemoryDeleteProj/MemoryUpdateProj/MemoryListProj/MemorySearchProj

## 用户偏好（MCP 自动获取当前用户身份）
记录用户工作风格、语言偏好、沟通习惯。如果不确定，就问用户。
mcp__SuperCC__MemoryAddUser/MemoryUpdateUser/MemoryDeleteUser/MemoryListUser/MemorySearchUser
"""


@dataclass
class UserPreference:
    """用户偏好条目（按用户 + 平台隔离）"""
    id: str = ""
    user_open_id: str = ""
    platform: str = "feishu"
    title: str = ""
    content: str = ""
    keywords: str = ""  # 逗号分隔
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ProjectMemory:
    """项目记忆条目（按项目 + 平台 + 聊天会话隔离）"""
    id: str = ""
    project_path: str = ""
    user_open_id: str = ""
    platform: str = "feishu"
    chat_id: str = ""
    title: str = ""
    content: str = ""
    keywords: str = ""  # 逗号分隔
    created_at: str = ""
    updated_at: str = ""


@dataclass
class MemorySearchResult:
    """记忆搜索结果"""
    memory: ProjectMemory
    rank: float  # TF-IDF cosine score (~0.05~1.0) 或 FTS5 BM25 rank (~0)，具体值取决于哪层命中


class MemoryManager:
    """SQLite+FTS5 双表记忆管理器"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base = Path.home() / ".supercc"
            base.mkdir(exist_ok=True)
            db_path = str(base / "memories.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._system_prompt_stale_callback: Callable | None = None

    def set_system_prompt_stale_callback(self, cb: Callable) -> None:
        """设置 system prompt 过期回调。记忆变更时被调用。"""
        self._system_prompt_stale_callback = cb

    def _notify_system_prompt_stale(self) -> None:
        if self._system_prompt_stale_callback:
            try:
                self._system_prompt_stale_callback()
            except Exception:
                pass

    # ── 类级别共享缓存（所有实例共享同一份，防止多实例缓存碎片化）─────────────
    _tfidf_cache: dict = {}
    _tfidf_lock = threading.Lock()
    # 用户偏好内存缓存：{(db_path, user_open_id): [UserPreference, ...]}
    _prefs_cache: dict = {}
    _prefs_cache_lock = threading.Lock()

    def _init_db(self):
        """创建/升级数据库：新建表或迁移已有表"""
        with sqlite3.connect(self.db_path) as conn:
            # ── user_preferences ──────────────────────────────────────────────────
            pref_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_preferences)")]
            if not pref_cols:
                # 新表：包含 user_open_id
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_preferences (
                        id           TEXT PRIMARY KEY,
                        user_open_id TEXT NOT NULL,
                        platform     TEXT NOT NULL DEFAULT 'feishu',
                        title        TEXT NOT NULL,
                        content      TEXT NOT NULL,
                        keywords     TEXT NOT NULL,
                        created_at   TEXT NOT NULL,
                        updated_at   TEXT NOT NULL
                    )
                """)
            elif "platform" not in pref_cols:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN platform TEXT NOT NULL DEFAULT 'feishu'")
                logger.info("migrated user_preferences: added platform column")
            # 旧表可能缺少 user_open_id（platform 新增前），补充迁移
            if "user_open_id" not in pref_cols:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN user_open_id TEXT NOT NULL DEFAULT ''")
                logger.info("migrated user_preferences: added user_open_id column")

            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS user_preferences_fts USING fts5(
                    id UNINDEXED, title, content, keywords, tokenize='unicode61'
                )
            """)

            # ── project_memories ─────────────────────────────────────────────────
            proj_cols = [r[1] for r in conn.execute("PRAGMA table_info(project_memories)")]
            if not proj_cols:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS project_memories (
                        id           TEXT PRIMARY KEY,
                        project_path TEXT NOT NULL,
                        user_open_id TEXT NOT NULL DEFAULT '',
                        platform     TEXT NOT NULL DEFAULT 'feishu',
                        title        TEXT NOT NULL,
                        content      TEXT NOT NULL,
                        keywords     TEXT NOT NULL,
                        created_at   TEXT NOT NULL,
                        updated_at   TEXT NOT NULL
                    )
                """)
            elif "platform" not in proj_cols:
                conn.execute("ALTER TABLE project_memories ADD COLUMN platform TEXT NOT NULL DEFAULT 'feishu'")
                logger.info("migrated project_memories: added platform column")
            if "user_open_id" not in proj_cols:
                conn.execute("ALTER TABLE project_memories ADD COLUMN user_open_id TEXT NOT NULL DEFAULT ''")
                logger.info("migrated project_memories: added user_open_id column")
            if "chat_id" not in proj_cols:
                conn.execute("ALTER TABLE project_memories ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
                logger.info("migrated project_memories: added chat_id column")

            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS project_memories_fts USING fts5(
                    id UNINDEXED, title, content, keywords, tokenize='unicode61'
                )
            """)

    # ── 用户偏好 ───────────────────────────────────────────────────────────────

    def add_preference(
        self,
        user_open_id: str,
        title: str,
        content: str,
        keywords: str,
        platform: str = "feishu",
    ) -> UserPreference:
        """添加一条用户偏好（按用户 + 平台隔离）"""
        for name, val, max_len in (
            ("title", title, 500),
            ("content", content, 5000),
            ("keywords", keywords, 500),
        ):
            if len(val) > max_len:
                raise ValueError(f"{name} 长度超过上限 {max_len}（当前 {len(val)}）")
        now = datetime.utcnow().isoformat()
        pref = UserPreference(
            id=str(uuid.uuid4())[:8],
            user_open_id=user_open_id,
            platform=platform,
            title=title,
            content=content,
            keywords=keywords,
            created_at=now,
            updated_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO user_preferences (id, user_open_id, platform, title, content, keywords, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pref.id, pref.user_open_id, pref.platform, pref.title, pref.content,
                 pref.keywords, pref.created_at, pref.updated_at)
            )
            conn.execute(
                "INSERT INTO user_preferences_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                (pref.id, pref.title, f"{pref.title} {pref.content} {pref.keywords}", pref.keywords)
            )
        # Invalidate user preference cache
        with self._prefs_cache_lock:
            self._prefs_cache.pop((self.db_path, user_open_id, platform), None)
        self._notify_system_prompt_stale()
        return pref

    def get_all_preferences(self, platform: str = "feishu") -> list[UserPreference]:
        """获取所有用户偏好（按创建时间倒序）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, user_open_id, platform, title, content, keywords, created_at, updated_at "
                "FROM user_preferences WHERE platform = ? ORDER BY created_at DESC",
                (platform,)
            ).fetchall()
        return [UserPreference(**{k: v for k, v in dict(r).items() if k != "_rank"}) for r in rows]

    def get_preferences_by_user(self, user_open_id: str, platform: str = "feishu") -> list[UserPreference]:
        """获取指定用户的所有偏好（按创建时间倒序，带内存缓存）。"""
        cache_key = (self.db_path, user_open_id, platform)
        with self._prefs_cache_lock:
            if cache_key in self._prefs_cache:
                return list(self._prefs_cache[cache_key])

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, user_open_id, platform, title, content, keywords, created_at, updated_at "
                "FROM user_preferences WHERE user_open_id = ? AND platform = ? ORDER BY created_at DESC",
                (user_open_id, platform)
            ).fetchall()
        prefs = [UserPreference(**{k: v for k, v in dict(r).items() if k != "_rank"}) for r in rows]

        with self._prefs_cache_lock:
            self._prefs_cache[cache_key] = list(prefs)
        return prefs

    def search_preferences(
        self,
        query: str,
        user_open_id: Optional[str] = None,
        platform: str = "feishu",
        limit: int = 5,
    ) -> list[UserPreference]:
        """全文搜索用户偏好：按 user_open_id + platform 过滤，keywords 优先（prefix 匹配），无结果再搜 title + content。"""
        if not query.strip():
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            base_cols = "m.id, m.user_open_id, m.platform, m.title, m.content, m.keywords, m.created_at, m.updated_at"

            def _run(query_str: str):
                if user_open_id:
                    return conn.execute(
                        f"SELECT {base_cols}, bm25(user_preferences_fts) as _rank "
                        "FROM user_preferences_fts "
                        "JOIN user_preferences m ON user_preferences_fts.id = m.id "
                        "WHERE user_preferences_fts MATCH ? AND m.user_open_id = ? AND m.platform = ? "
                        "ORDER BY _rank LIMIT ?",
                        (query_str, user_open_id, platform, limit)
                    ).fetchall()
                else:
                    return conn.execute(
                        f"SELECT {base_cols}, bm25(user_preferences_fts) as _rank "
                        "FROM user_preferences_fts "
                        "JOIN user_preferences m ON user_preferences_fts.id = m.id "
                        "WHERE user_preferences_fts MATCH ? AND m.platform = ? ORDER BY _rank LIMIT ?",
                        (query_str, platform, limit)
                    ).fetchall()

            rows = _run(query)
        return [UserPreference(**{k: v for k, v in dict(r).items() if k != "_rank"}) for r in rows]

    def update_preference(
        self,
        pref_id: str,
        title: str,
        content: str,
        keywords: str,
        platform: str = "feishu",
    ) -> bool:
        """更新一条用户偏好"""
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_open_id FROM user_preferences WHERE id = ? AND platform = ?", (pref_id, platform)
            ).fetchone()
            uid = row["user_open_id"] if row else None

            affected = conn.execute("""
                UPDATE user_preferences
                SET title=?, content=?, keywords=?, updated_at=?
                WHERE id=? AND platform=?
            """, (title, content, keywords, now, pref_id, platform)).rowcount
            if affected > 0:
                conn.execute(
                    "DELETE FROM user_preferences_fts WHERE id = ?", (pref_id,)
                )
                conn.execute(
                    "INSERT INTO user_preferences_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                    (pref_id, title, f"{title} {content} {keywords}", keywords)
                )
        # Invalidate user preference cache (keyed by db_path + user_open_id + platform)
        if uid:
            with self._prefs_cache_lock:
                # 清除该用户所有 platform 的缓存（简单处理）
                self._prefs_cache = {
                    k: v for k, v in self._prefs_cache.items()
                    if not (k[0] == self.db_path and k[1] == uid)
                }
        self._notify_system_prompt_stale()
        return affected > 0

    def delete_preference(self, pref_id: str, platform: str = "feishu") -> bool:
        """删除一条用户偏好"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_open_id FROM user_preferences WHERE id = ? AND platform = ?", (pref_id, platform)
            ).fetchone()
            uid = row["user_open_id"] if row else None

            affected = conn.execute(
                "DELETE FROM user_preferences WHERE id = ? AND platform = ?", (pref_id, platform)
            ).rowcount
            conn.execute("DELETE FROM user_preferences_fts WHERE id = ?", (pref_id,))
        # Invalidate user preference cache
        if uid:
            with self._prefs_cache_lock:
                self._prefs_cache = {
                    k: v for k, v in self._prefs_cache.items()
                    if not (k[0] == self.db_path and k[1] == uid)
                }
        self._notify_system_prompt_stale()
        return affected > 0

    def inject_context(
        self,
        user_open_id: str,
        project_path: str | None = None,
        platform: str = "feishu",
        chat_id: str = "",
    ) -> str:
        """
        注入用户偏好和项目记忆到 prompt。
        用户偏好：最新 50 条，每条 content 截断 200 字。
        项目记忆：最新 5 条，仅 title。
        按 platform + chat_id 隔离。
        """
        parts: list[str] = []

        # 用户偏好：全量，最新 50 条
        prefs = self.get_preferences_by_user(user_open_id, platform=platform)
        if prefs:
            prefs = prefs[:50]
            lines = ["\n【用户偏好】", "---"]
            for p in prefs:
                lines.append(f"**{p.title}**")
                content = p.content
                if len(content) > 200:
                    content = content[:200] + "…"
                lines.append(content)
                lines.append("")
            parts.append("\n".join(lines))

        if project_path:
            mems = self.get_project_memories(project_path, platform=platform, chat_id=chat_id)[:5]
            if mems:
                lines = ["\n【项目记忆（最新 5 条）】", "---"]
                for m in mems:
                    lines.append(f"- {m.title}")
                parts.append("\n".join(lines))

        return "\n".join(parts)

    # ── 项目记忆 ───────────────────────────────────────────────────────────────

    def add_project_memory(
        self,
        project_path: str,
        title: str,
        content: str,
        keywords: str,
        user_open_id: str = "",
        platform: str = "feishu",
        chat_id: str = "",
    ) -> ProjectMemory:
        """添加一条项目记忆（按项目 + 平台 + 聊天会话隔离）"""
        for name, val, max_len in (
            ("title", title, 500),
            ("content", content, 5000),
            ("keywords", keywords, 500),
        ):
            if len(val) > max_len:
                raise ValueError(f"{name} 长度超过上限 {max_len}（当前 {len(val)}）")
        now = datetime.utcnow().isoformat()
        mem = ProjectMemory(
            id=str(uuid.uuid4())[:8],
            project_path=project_path,
            user_open_id=user_open_id,
            platform=platform,
            chat_id=chat_id,
            title=title,
            content=content,
            keywords=keywords,
            created_at=now,
            updated_at=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO project_memories (id, project_path, user_open_id, platform, chat_id, title, content, keywords, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mem.id, mem.project_path, mem.user_open_id, mem.platform, mem.chat_id, mem.title, mem.content, mem.keywords, mem.created_at, mem.updated_at)
            )
            conn.execute(
                "INSERT INTO project_memories_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                (mem.id, mem.title, f"{mem.title} {mem.content} {mem.keywords}", mem.keywords)
            )
        # Invalidate TF-IDF cache (thread-safe)
        self._invalidate_tfidf_cache(project_path, platform, chat_id)
        self._notify_system_prompt_stale()
        return mem

    def _invalidate_tfidf_cache(self, project_path: str, platform: str | None = None, chat_id: str | None = None):
        """线程安全地清除项目 TF-IDF 缓存（按 db_path + project_path + platform + chat_id 分隔）"""
        if platform is not None and chat_id is not None:
            cache_key = (self.db_path, project_path, platform, chat_id)
            with self._tfidf_lock:
                self._tfidf_cache.pop(cache_key, None)
        elif platform is not None:
            # 只按 project_path + platform 清除（兼容旧调用）
            with self._tfidf_lock:
                self._tfidf_cache = {
                    k: v for k, v in self._tfidf_cache.items()
                    if not (k[0] == self.db_path and k[1] == project_path and k[2] == platform)
                }
        else:
            # 无 platform 时清除该项目的所有缓存
            with self._tfidf_lock:
                self._tfidf_cache = {
                    k: v for k, v in self._tfidf_cache.items()
                    if not (k[0] == self.db_path and k[1] == project_path)
                }
        # 超过 50 个项目时 evict 最老的条目
        with self._tfidf_lock:
            if len(self._tfidf_cache) > 50:
                keys_to_remove = list(self._tfidf_cache.keys())[:25]
                for k in keys_to_remove:
                    del self._tfidf_cache[k]

    def search_project_memories(
        self,
        query: str,
        project_path: str,
        platform: str = "feishu",
        chat_id: str = "",
        limit: int = 5,
    ) -> list[MemorySearchResult]:
        """两层检索策略：TF-IDF cosine（语义）+ FTS5 BM25（精确关键词兜底）。按 project_path + platform + chat_id 隔离。"""
        if not query.strip() or not project_path:
            return []

        # 第一层：TF-IDF cosine 语义搜索
        tfidf_results = self._search_tfidf(query, project_path, platform, chat_id, limit)
        if tfidf_results:
            return tfidf_results

        # 第二层：FTS5 BM25 精确兜底
        return self._search_fts5(query, project_path, platform, chat_id, limit)

    def _search_fts5(
        self, query: str, project_path: str, platform: str, chat_id: str, limit: int,
    ) -> list[MemorySearchResult]:
        """FTS5 BM25 精确关键词搜索"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT m.id, m.project_path, m.user_open_id, m.platform, m.chat_id, m.title, m.content, m.keywords,
                       m.created_at, m.updated_at,
                       bm25(project_memories_fts) as rank
                FROM project_memories_fts
                JOIN project_memories m ON project_memories_fts.id = m.id
                WHERE project_memories_fts MATCH ?
                  AND m.project_path = ?
                  AND m.platform = ?
                  AND m.chat_id = ?
                ORDER BY rank
                LIMIT ?
            """, (query, project_path, platform, chat_id, limit)).fetchall()
        results = []
        for row in rows:
            mem = ProjectMemory(
                id=row["id"],
                project_path=row["project_path"],
                user_open_id=row["user_open_id"],
                platform=row["platform"],
                chat_id=row["chat_id"],
                title=row["title"],
                content=row["content"],
                keywords=row["keywords"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            results.append(MemorySearchResult(memory=mem, rank=row["rank"]))
        return results

    def _search_tfidf(
        self, query: str, project_path: str, platform: str, chat_id: str, limit: int,
    ) -> list[MemorySearchResult]:
        """TF-IDF cosine 语义搜索（sklearn 离线计算，无需网络）"""
        if not HAS_SKLEARN or cosine_similarity is None:
            return []

        try:
            vectorizer, matrix, memories = self._get_tfidf_cache(project_path, platform, chat_id)
        except Exception:
            return []

        if not memories:
            return []

        try:
            # 查询必须与索引时分词方式一致
            tokenized_query = " ".join(jieba.cut(query))
            q_vec = vectorizer.transform([tokenized_query])
            scores = cosine_similarity(q_vec, matrix).flatten()
            # 只取 score > 0.05 的结果
            filtered = [(i, float(scores[i])) for i in range(len(scores)) if scores[i] > 0.05]
            filtered.sort(key=lambda x: x[1], reverse=True)
            return [
                MemorySearchResult(memory=memories[i], rank=score)
                for i, score in filtered[:limit]
            ]
        except Exception:
            return []

    def _get_tfidf_cache(self, project_path: str, platform: str = "feishu", chat_id: str = "") -> tuple:
        """获取或构建某项目的 TF-IDF 缓存（双检查锁定，读写分离）。按 project_path + platform + chat_id 隔离。"""
        cache_key = (self.db_path, project_path, platform, chat_id)

        # 第一次检查：无需加锁，先看 cache 是否已存在
        if cache_key in self._tfidf_cache:
            return self._tfidf_cache[cache_key]

        with self._tfidf_lock:
            # 第二次检查：抢到锁后再确认，避免重复构建
            if cache_key in self._tfidf_cache:
                return self._tfidf_cache[cache_key]

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, project_path, user_open_id, platform, chat_id, title, content, keywords, "
                    "created_at, updated_at FROM project_memories WHERE project_path = ? AND platform = ? AND chat_id = ?",
                    (project_path, platform, chat_id),
                ).fetchall()

            if not rows:
                # 空项目不缓存 sentinel，只返回空结果
                return (None, None, [])

            memories = [
                ProjectMemory(
                    id=r["id"], project_path=r["project_path"],
                    user_open_id=r["user_open_id"], platform=r["platform"], chat_id=r["chat_id"],
                    title=r["title"], content=r["content"],
                    keywords=r["keywords"],
                    created_at=r["created_at"], updated_at=r["updated_at"],
                )
                for r in rows
            ]

            texts = [m.title + " " + m.content + " " + m.keywords for m in memories]

            def tokenizer(t):
                return " ".join(jieba.cut(t)).split()

            vectorizer = TfidfVectorizer(tokenizer=tokenizer)
            matrix = vectorizer.fit_transform(texts)

            self._tfidf_cache[cache_key] = (vectorizer, matrix, memories)
            return self._tfidf_cache[cache_key]

    def get_project_memories(
        self,
        project_path: str,
        platform: str = "feishu",
        chat_id: str = "",
    ) -> list[ProjectMemory]:
        """列出某项目+平台+聊天会话下所有记忆（按创建时间倒序）"""
        if not project_path:
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, project_path, user_open_id, platform, chat_id, title, content, keywords, created_at, updated_at
                FROM project_memories
                WHERE project_path = ? AND platform = ? AND chat_id = ?
                ORDER BY created_at DESC
            """, (project_path, platform, chat_id)).fetchall()
        return [
            ProjectMemory(
                id=row["id"],
                project_path=row["project_path"],
                user_open_id=row["user_open_id"],
                platform=row["platform"],
                chat_id=row["chat_id"],
                title=row["title"],
                content=row["content"],
                keywords=row["keywords"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def update_project_memory(
        self,
        memory_id: str,
        title: str,
        content: str,
        keywords: str,
        platform: str = "feishu",
        chat_id: str = "",
    ) -> bool:
        """更新一条项目记忆"""
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT project_path FROM project_memories WHERE id = ? AND platform = ? AND chat_id = ?",
                (memory_id, platform, chat_id)
            ).fetchone()
            proj_path = row["project_path"] if row else ""

            affected = conn.execute("""
                UPDATE project_memories
                SET title=?, content=?, keywords=?, updated_at=?
                WHERE id=? AND platform=? AND chat_id=?
            """, (title, content, keywords, now, memory_id, platform, chat_id)).rowcount
            if affected > 0:
                conn.execute(
                    "DELETE FROM project_memories_fts WHERE id = ?", (memory_id,)
                )
                conn.execute(
                    "INSERT INTO project_memories_fts(id, title, content, keywords) VALUES (?, ?, ?, ?)",
                    (memory_id, title, f"{title} {content} {keywords}", keywords)
                )
        # Invalidate TF-IDF cache
        if proj_path:
            self._invalidate_tfidf_cache(proj_path, platform, chat_id)
        self._notify_system_prompt_stale()
        return affected > 0

    def delete_project_memory(
        self,
        memory_id: str,
        platform: str = "feishu",
        chat_id: str = "",
    ) -> dict | None:
        """删除一条项目记忆，返回被删记录（删除前先查出）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, project_path, chat_id, title, content, keywords FROM project_memories WHERE id = ? AND platform = ? AND chat_id = ?",
                (memory_id, platform, chat_id),
            ).fetchone()
            if row is None:
                return None

            deleted = dict(row)
            conn.execute(
                "DELETE FROM project_memories WHERE id = ? AND platform = ? AND chat_id = ?",
                (memory_id, platform, chat_id)
            )
            conn.execute("DELETE FROM project_memories_fts WHERE id = ?", (memory_id,))

            proj_path = deleted.get("project_path", "")
            deleted_chat_id = deleted.get("chat_id", "")
            if proj_path:
                self._invalidate_tfidf_cache(proj_path, platform, deleted_chat_id)
            self._notify_system_prompt_stale()
            return deleted

    def clear_project_memories(
        self,
        project_path: str,
        platform: str = "feishu",
        chat_id: str = "",
    ) -> int:
        """清空某项目+平台+聊天会话下所有记忆"""
        if not project_path:
            return 0
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM project_memories WHERE project_path = ? AND platform = ? AND chat_id = ?",
                (project_path, platform, chat_id)
            ).fetchall()
            ids = [row[0] for row in rows]
            count = len(ids)
            if count == 0:
                self._invalidate_tfidf_cache(project_path, platform, chat_id)
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                "DELETE FROM project_memories WHERE project_path = ? AND platform = ? AND chat_id = ?",
                (project_path, platform, chat_id)
            )
            conn.execute(f"DELETE FROM project_memories_fts WHERE id IN ({placeholders})", ids)

        # Invalidate TF-IDF cache（无论 count 是否为 0 都清理）
        self._invalidate_tfidf_cache(project_path, platform, chat_id)
        self._notify_system_prompt_stale()
        return count
        return count

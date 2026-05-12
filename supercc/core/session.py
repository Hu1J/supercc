"""SQLite-based SessionManager with four-key isolation: bot_id × project_path × platform × chat_id."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from supercc.core.protocol import SessionKey

_CST = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)


# ── 路径 ─────────────────────────────────────────────────────────────────────

DEFAULT_SESSIONS_DB_PATH = str(Path.home() / ".supercc" / "sessions.db")


# ── 数据模型 ─────────────────────────────────────────────────────────────────

@dataclass
class Session:
    """会话数据模型（对应数据库 sessions 表）。"""
    session_id: str
    bot_id: str
    project_path: str
    platform: str
    chat_id: str
    user_open_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(_CST))
    last_used: datetime = field(default_factory=lambda: datetime.now(_CST))
    total_cost: float = 0.0
    message_count: int = 0
    # 平台特定字段
    sdk_session_id: Optional[str] = None
    last_message_at: Optional[datetime] = None
    proactive_today_count: int = 0
    proactive_today_date: Optional[str] = None  # YYYY-MM-DD
    last_proactive_at: Optional[datetime] = None
    group_members: Optional[str] = None  # JSON string


# ── SessionManager ────────────────────────────────────────────────────────────

class SessionManager:
    """
    线程安全的 SQLite Session 管理器。

    使用四元组 SessionKey (bot_id × project_path × platform × chat_id) 做隔离。
    数据库路径: ~/.supercc/sessions.db（核心和插件共享）。
    """

    def __init__(self, db_path: str = DEFAULT_SESSIONS_DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 数据库初始化 ─────────────────────────────────────────────────────────

    def _migrate_add_column(self, conn: sqlite3.Connection, table: str, column: str, dtype: str):
        """安全地新增列（对已存在的老数据库友好）。"""
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # sessions 表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    bot_id     TEXT NOT NULL DEFAULT '',
                    project_path  TEXT NOT NULL DEFAULT '',
                    platform   TEXT NOT NULL DEFAULT 'feishu',
                    chat_id    TEXT NOT NULL DEFAULT '',
                    user_open_id TEXT,
                    created_at TIMESTAMP NOT NULL,
                    last_used  TIMESTAMP NOT NULL,
                    total_cost REAL DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    sdk_session_id TEXT,
                    last_message_at TIMESTAMP,
                    proactive_today_count INTEGER DEFAULT 0,
                    proactive_today_date TEXT,
                    last_proactive_at TIMESTAMP,
                    group_members TEXT
                )
            """)
            # 旧数据库迁移
            self._migrate_add_column(conn, "sessions", "bot_id", "TEXT NOT NULL DEFAULT ''")
            self._migrate_add_column(conn, "sessions", "project_path", "TEXT NOT NULL DEFAULT ''")
            self._migrate_add_column(conn, "sessions", "user_open_id", "TEXT")
            self._migrate_add_column(conn, "sessions", "group_members", "TEXT")
            self._migrate_add_column(conn, "sessions", "user_id", "TEXT")

            # 索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_lookup
                ON sessions(bot_id, project_path, platform, chat_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_platform
                ON sessions(user_open_id, platform)
            """)

            # messages 表（对话历史，用于记忆提取）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  TEXT NOT NULL UNIQUE,
                    session_id  TEXT NOT NULL,
                    chat_id     TEXT NOT NULL,
                    user_open_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    content     TEXT,
                    direction   TEXT NOT NULL DEFAULT 'incoming',
                    created_at  TIMESTAMP NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at)"
            )

    # ── 会话 CRUD ────────────────────────────────────────────────────────────

    def get_or_create_session(self, key: SessionKey, user_open_id: str) -> Session:
        """
        按 SessionKey 获取或创建会话。

        查找优先级: bot_id + project_path + platform + chat_id 完全匹配 > 创建新会话。
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE bot_id = ? AND project_path = ? AND platform = ? AND chat_id = ?
                   ORDER BY last_used DESC LIMIT 1""",
                (key.bot_id, key.project_path, key.platform, key.chat_id),
            ).fetchone()

            if row:
                return self._row_to_session(row)

        # 不存在则创建
        return self._create_session(key, user_open_id)

    def _create_session(self, key: SessionKey, user_open_id: str) -> Session:
        now = datetime.now(_CST)
        session = Session(
            session_id=f"session_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            bot_id=key.bot_id,
            project_path=key.project_path,
            platform=key.platform,
            chat_id=key.chat_id,
            user_open_id=user_open_id,
            created_at=now,
            last_used=now,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, bot_id, project_path, platform, chat_id, user_id, user_open_id,
                    created_at, last_used, total_cost, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id, session.bot_id, session.project_path,
                    session.platform, session.chat_id, user_open_id, session.user_open_id,
                    session.created_at.isoformat(), session.last_used.isoformat(),
                    session.total_cost, session.message_count,
                ),
            )
        return session

    def get_session_by_id(self, session_id: str) -> Optional[Session]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row:
            return self._row_to_session(row)
        return None

    def update_session(
        self,
        session_id: str,
        cost: float = 0,
        message_increment: int = 0,
        update_last_message: bool = False,
    ):
        """更新会话统计。"""
        with sqlite3.connect(self.db_path) as conn:
            if update_last_message:
                conn.execute(
                    """UPDATE sessions
                       SET last_used = ?,
                           total_cost = total_cost + ?,
                           message_count = message_count + ?,
                           last_message_at = ?
                       WHERE session_id = ?""",
                    (
                        datetime.now(_CST).isoformat(),
                        cost, message_increment,
                        datetime.now(_CST).isoformat(),
                        session_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE sessions
                       SET last_used = ?,
                           total_cost = total_cost + ?,
                           message_count = message_count + ?
                       WHERE session_id = ?""",
                    (datetime.now(_CST).isoformat(), cost, message_increment, session_id),
                )

    def delete_session(self, session_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # ── 便捷查找 ────────────────────────────────────────────────────────────

    def get_active_session(
        self,
        bot_id: str,
        project_path: str,
        platform: str,
        chat_id: str,
    ) -> Optional[Session]:
        """按四元组精确查找最近活跃的会话。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE bot_id = ? AND project_path = ? AND platform = ? AND chat_id = ?
                   ORDER BY last_used DESC LIMIT 1""",
                (bot_id, project_path, platform, chat_id),
            ).fetchone()
        if row:
            return self._row_to_session(row)
        return None

    # ── 消息存储 ────────────────────────────────────────────────────────────

    def store_message(
        self,
        message_id: str,
        session_id: str,
        chat_id: str,
        user_open_id: str,
        message_type: str,
        raw_content: str,
        content: Optional[str] = None,
        direction: str = "incoming",
    ):
        """存储一条消息（用于记忆提取）。"""
        now = datetime.now(_CST).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, session_id, chat_id, user_open_id, message_type,
                    raw_content, content, direction, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, session_id, chat_id, user_open_id, message_type,
                 raw_content, content, direction, now),
            )

    def get_recent_messages(self, session_id: str, limit: int = 20) -> list[dict]:
        """获取最近 N 条消息（用于发送给 AI）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM messages
                   WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            bot_id=row["bot_id"] or "",
            project_path=row["project_path"] or "",
            platform=row["platform"] or "feishu",
            chat_id=row["chat_id"] or "",
            user_open_id=row["user_open_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used=datetime.fromisoformat(row["last_used"]),
            total_cost=row["total_cost"],
            message_count=row["message_count"],
            sdk_session_id=row["sdk_session_id"],
            last_message_at=datetime.fromisoformat(row["last_message_at"])
                if row["last_message_at"] else None,
            proactive_today_count=row["proactive_today_count"],
            proactive_today_date=row["proactive_today_date"],
            last_proactive_at=datetime.fromisoformat(row["last_proactive_at"])
                if row["last_proactive_at"] else None,
            group_members=row["group_members"],
        )

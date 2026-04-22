"""SQLite-based session manager for Claude Code conversations."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    sdk_session_id: str | None
    user_id: str
    project_path: str
    created_at: datetime
    last_used: datetime
    total_cost: float
    message_count: int
    chat_id: str | None = None   # 新增：最近活跃的飞书 chat_id
    last_message_at: datetime | None = None
    proactive_today_count: int = 0
    proactive_today_date: str | None = None   # YYYY-MM-DD 格式
    last_proactive_at: datetime | None = None  # 发完主动推送后，记录时间戳，用于冷却期判断


class SessionManager:
    def __init__(self, db_path: str):
        self._conv_history: dict[str, list[str]] = {}
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._init_memories_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    sdk_session_id TEXT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT,
                    project_path TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    last_used TIMESTAMP NOT NULL,
                    total_cost REAL DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    last_message_at TIMESTAMP,
                    proactive_today_count INTEGER DEFAULT 0,
                    proactive_today_date TEXT
                )
            """)
            # Migrate: add sdk_session_id column if it doesn't exist (existing installs)
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN sdk_session_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migrate: add chat_id column if it doesn't exist (existing installs)
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN chat_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migrate: add last_message_at column if it doesn't exist
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN last_message_at TIMESTAMP")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migrate: add proactive_today_count column if it doesn't exist
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN proactive_today_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # Migrate: add proactive_today_date column if it doesn't exist
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN proactive_today_date TEXT")
            except sqlite3.OperationalError:
                pass
            # Migrate: add last_proactive_at column if it doesn't exist
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN last_proactive_at TIMESTAMP")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_last
                ON sessions(user_id, last_used DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_open_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'incoming'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at)"
            )

    def create_session(
        self,
        user_id: str,
        project_path: str,
        sdk_session_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> Session:
        """Create a new session for a user.

        For group chat, pass chat_id (and optionally thread_id) to enable
        session isolation per chat. Session key is: user_id + chat_id (+ thread_id).
        """
        now = datetime.utcnow()
        session = Session(
            session_id=f"session_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            sdk_session_id=sdk_session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=now,
            last_used=now,
            total_cost=0.0,
            message_count=0,
            chat_id=chat_id or "",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, sdk_session_id, user_id, chat_id, project_path, created_at, last_used, total_cost, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.sdk_session_id,
                    session.user_id,
                    session.chat_id,
                    session.project_path,
                    session.created_at.isoformat(),
                    session.last_used.isoformat(),
                    session.total_cost,
                    session.message_count,
                ),
            )
        return session

    def get_active_session_for_chat(self, user_id: str, chat_id: str) -> Optional[Session]:
        """Get the most recent session for a user in a specific chat (group or p2p).

        This enables session isolation per chat — group chat sessions are separate
        from p2p sessions even for the same user.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE user_id = ? AND chat_id = ?
                   ORDER BY last_used DESC
                   LIMIT 1""",
                (user_id, chat_id),
            ).fetchone()
        if row:
            return Session(
                session_id=row["session_id"],
                sdk_session_id=row["sdk_session_id"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                project_path=row["project_path"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_used=datetime.fromisoformat(row["last_used"]),
                total_cost=row["total_cost"],
                message_count=row["message_count"],
                last_message_at=datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
                proactive_today_count=row["proactive_today_count"],
                proactive_today_date=row["proactive_today_date"],
                last_proactive_at=datetime.fromisoformat(row["last_proactive_at"]) if row["last_proactive_at"] else None,
            )
        return None

    def get_active_session(self, user_id: str) -> Optional[Session]:
        """Get the most recent session for a user."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE user_id = ?
                   ORDER BY last_used DESC
                   LIMIT 1""",
                (user_id,),
            ).fetchone()
        if row:
            return Session(
                session_id=row["session_id"],
                sdk_session_id=row["sdk_session_id"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                project_path=row["project_path"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_used=datetime.fromisoformat(row["last_used"]),
                total_cost=row["total_cost"],
                message_count=row["message_count"],
                last_message_at=datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
                proactive_today_count=row["proactive_today_count"],
                proactive_today_date=row["proactive_today_date"],
                last_proactive_at=datetime.fromisoformat(row["last_proactive_at"]) if row["last_proactive_at"] else None,
            )
        return None

    def update_session(
        self,
        session_id: str,
        cost: float = 0,
        message_increment: int = 0,
        update_last_message: bool = False,
    ):
        """Update session stats after a conversation turn."""
        with sqlite3.connect(self.db_path) as conn:
            if update_last_message:
                conn.execute(
                    """UPDATE sessions
                       SET last_used = ?,
                           total_cost = total_cost + ?,
                           message_count = message_count + ?,
                           last_message_at = ?
                       WHERE session_id = ?""",
                    (datetime.utcnow().isoformat(), cost, message_increment,
                     datetime.utcnow().isoformat(), session_id),
                )
            else:
                conn.execute(
                    """UPDATE sessions
                       SET last_used = ?,
                           total_cost = total_cost + ?,
                           message_count = message_count + ?
                       WHERE session_id = ?""",
                    (datetime.utcnow().isoformat(), cost, message_increment, session_id),
                )

    def update_sdk_session_id(self, session_id: str, sdk_session_id: str) -> None:
        """Store the SDK's session ID for future continue_session calls."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE sessions SET sdk_session_id = ? WHERE session_id = ?""",
                (sdk_session_id, session_id),
            )

    def delete_session(self, session_id: str):
        """Delete a session."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def get_active_session_by_chat_id(self) -> Optional[Session]:
        """Get the most recent session that has a chat_id set."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE chat_id IS NOT NULL AND chat_id != ''
                   ORDER BY last_used DESC
                   LIMIT 1""",
            ).fetchone()
            if row:
                return Session(
                    session_id=row["session_id"],
                    sdk_session_id=row["sdk_session_id"],
                    user_id=row["user_id"],
                    chat_id=row["chat_id"],
                    project_path=row["project_path"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_used=datetime.fromisoformat(row["last_used"]),
                    total_cost=row["total_cost"],
                    message_count=row["message_count"],
                    last_message_at=datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
                    proactive_today_count=row["proactive_today_count"],
                    proactive_today_date=row["proactive_today_date"],
                    last_proactive_at=datetime.fromisoformat(row["last_proactive_at"]) if row["last_proactive_at"] else None,
                )
            return None

    def update_chat_id(self, user_id: str, chat_id: str) -> None:
        """Update the chat_id for the most recent session of a user."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE sessions
                   SET chat_id = ?
                   WHERE session_id = (
                       SELECT session_id FROM sessions
                       WHERE user_id = ?
                       ORDER BY last_used DESC
                       LIMIT 1
                   )""",
                (chat_id, user_id),
            )

    def get_all_users(self) -> list[Session]:
        """Get all sessions with last_message_at info for proactive check."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE last_message_at IS NOT NULL
                   ORDER BY last_used DESC"""
            ).fetchall()
        return [
            Session(
                session_id=row["session_id"],
                sdk_session_id=row["sdk_session_id"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                project_path=row["project_path"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_used=datetime.fromisoformat(row["last_used"]),
                total_cost=row["total_cost"],
                message_count=row["message_count"],
                last_message_at=datetime.fromisoformat(row["last_message_at"]),
                proactive_today_count=row["proactive_today_count"],
                proactive_today_date=row["proactive_today_date"],
                last_proactive_at=datetime.fromisoformat(row["last_proactive_at"]) if row["last_proactive_at"] else None,
            )
            for row in rows
        ]

    def bump_proactive_count(self, session_id: str) -> None:
        """Increment proactive count for the day, reset if new day."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE sessions
                   SET proactive_today_count = proactive_today_count + 1,
                       proactive_today_date = ?
                   WHERE session_id = ?""",
                (today, session_id),
            )

    def update_last_proactive_at(self, session_id: str) -> None:
        """Record the timestamp when a proactive message was sent (for cooldown tracking)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE sessions SET last_proactive_at = ? WHERE session_id = ?""",
                (datetime.utcnow().isoformat(), session_id),
            )

    def store_message(
        self,
        message_id: str,
        session_id: str,
        chat_id: str,
        user_open_id: str,
        message_type: str,
        raw_content: str,
        content: str | None = None,
        direction: str = "incoming",
    ) -> None:
        """Store an incoming or outgoing message for memory enhancement."""
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, session_id, chat_id, user_open_id, message_type,
                    raw_content, content, created_at, direction)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, session_id, chat_id, user_open_id, message_type,
                 raw_content, content, now, direction),
            )
        # Track conversation for auto memory extraction
        if direction == "incoming":
            history = self._conv_history.setdefault(session_id, [])
            history.append(content or raw_content)
            self._conv_history[session_id] = history[-20:]  # keep last 20 messages

    def _init_memories_db(self):
        """Initialize memories DB (separate file from sessions)."""
        from cc_feishu_bridge.claude.memory_manager import get_memory_manager
        # Initialise the memories DB lazily — get_memory_manager creates the file
        # in ~/.cc-feishu-bridge/ on first access.
        try:
            get_memory_manager()
        except Exception:
            logger.exception("Failed to init memories DB")
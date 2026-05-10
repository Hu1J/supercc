"""Tests for core/session.py."""

import os
import pytest
import tempfile
from datetime import datetime

from core.protocol import SessionKey
from core.session import SessionManager, DEFAULT_SESSIONS_DB_PATH


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except Exception:
        pass


@pytest.fixture
def manager(db_path):
    return SessionManager(db_path=db_path)


class TestSessionManager:
    def test_get_or_create_session_new(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session = manager.get_or_create_session(key, user_open_id="user_1")
        assert session is not None
        assert session.bot_id == "bot_123"
        assert session.project_path == "/Users/test/project"
        assert session.platform == "feishu"
        assert session.chat_id == "oc_abc123"
        assert session.user_open_id == "user_1"
        assert session.session_id.startswith("session_")

    def test_get_or_create_session_reuse(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session1 = manager.get_or_create_session(key, user_open_id="user_1")
        session2 = manager.get_or_create_session(key, user_open_id="user_1")
        # Same key should return same session
        assert session1.session_id == session2.session_id

    def test_different_keys_different_sessions(self, manager):
        key1 = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        key2 = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_different",
        )
        session1 = manager.get_or_create_session(key1, user_open_id="user_1")
        session2 = manager.get_or_create_session(key2, user_open_id="user_1")
        assert session1.session_id != session2.session_id

    def test_get_active_session(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        created = manager.get_or_create_session(key, user_open_id="user_1")
        found = manager.get_active_session(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        assert found is not None
        assert found.session_id == created.session_id

    def test_get_active_session_not_found(self, manager):
        found = manager.get_active_session(
            bot_id="nonexistent",
            project_path="/nonexistent",
            platform="feishu",
            chat_id="oc_xxx",
        )
        assert found is None

    def test_update_session(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session = manager.get_or_create_session(key, user_open_id="user_1")
        manager.update_session(
            session_id=session.session_id,
            cost=0.05,
            message_increment=2,
            update_last_message=True,
        )
        updated = manager.get_session_by_id(session.session_id)
        assert updated is not None
        assert updated.total_cost == 0.05
        assert updated.message_count == 2
        assert updated.last_message_at is not None

    def test_delete_session(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session = manager.get_or_create_session(key, user_open_id="user_1")
        manager.delete_session(session.session_id)
        found = manager.get_session_by_id(session.session_id)
        assert found is None

    def test_store_message(self, manager):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session = manager.get_or_create_session(key, user_open_id="user_1")
        manager.store_message(
            message_id="msg_1",
            session_id=session.session_id,
            chat_id="oc_abc123",
            user_open_id="user_1",
            message_type="text",
            raw_content="hello",
            content="hello",
            direction="incoming",
        )
        messages = manager.get_recent_messages(session.session_id, limit=10)
        assert len(messages) == 1
        assert messages[0]["content"] == "hello"

    def test_bot_id_isolation(self, manager):
        """Different bot_ids should have different sessions."""
        key1 = SessionKey(
            bot_id="bot_1",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        key2 = SessionKey(
            bot_id="bot_2",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        session1 = manager.get_or_create_session(key1, user_open_id="user_1")
        session2 = manager.get_or_create_session(key2, user_open_id="user_1")
        assert session1.session_id != session2.session_id

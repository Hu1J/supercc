"""Tests for user_open_id support in memory_manager."""
import os
import tempfile
from pathlib import Path

import pytest

from cc_feishu_bridge.claude.memory_manager import MemoryManager


@pytest.fixture
def mm():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "memories.db")
        m = MemoryManager(db_path)
        yield m


def test_add_preference_requires_user_open_id(mm):
    """add_preference stores user_open_id correctly."""
    p = mm.add_preference(
        user_open_id="ou_abc123",
        title="测试偏好",
        content="测试内容",
        keywords="测试,关键字",
    )
    assert p.user_open_id == "ou_abc123"
    assert p.title == "测试偏好"


def test_inject_context_filters_by_user(mm):
    """inject_context returns only the requesting user's preferences."""
    mm.add_preference("ou_user1", "偏好1", "内容1", "关键1")
    mm.add_preference("ou_user2", "偏好2", "内容2", "关键2")

    ctx1 = mm.inject_context("ou_user1")
    ctx2 = mm.inject_context("ou_user2")

    assert "偏好1" in ctx1
    assert "偏好2" not in ctx1
    assert "偏好2" in ctx2
    assert "偏好1" not in ctx2


def test_get_preferences_by_user(mm):
    """get_preferences_by_user returns only matching user."""
    mm.add_preference("ou_user1", "标题A", "内容A", "词A")
    mm.add_preference("ou_user1", "标题B", "内容B", "词B")
    mm.add_preference("ou_user2", "标题C", "内容C", "词C")

    prefs_u1 = mm.get_preferences_by_user("ou_user1")
    prefs_u2 = mm.get_preferences_by_user("ou_user2")

    assert len(prefs_u1) == 2
    assert len(prefs_u2) == 1
    assert prefs_u2[0].title == "标题C"


def test_inject_context_empty_for_unknown_user(mm):
    """inject_context returns empty string for unknown user."""
    mm.add_preference("ou_known", "已知用户", "内容", "kw")
    ctx = mm.inject_context("ou_unknown")
    assert ctx == ""


def test_search_preferences_by_user(mm):
    """search_preferences filters by user_open_id."""
    mm.add_preference("ou_alice", "狗蛋规则", "发版要检查", "发版,狗蛋")
    mm.add_preference("ou_bob", "狗蛋规则", "发版要检查", "发版,狗蛋")  # same content, different user

    results_alice = mm.search_preferences("发版", user_open_id="ou_alice")
    results_bob = mm.search_preferences("发版", user_open_id="ou_bob")
    results_all = mm.search_preferences("发版")

    assert len(results_alice) >= 1
    assert all(r.user_open_id == "ou_alice" for r in results_alice)
    assert len(results_bob) >= 1
    assert all(r.user_open_id == "ou_bob" for r in results_bob)
    # No user_open_id filter → may return both
    assert len(results_all) >= 2

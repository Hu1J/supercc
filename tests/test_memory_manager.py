import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile
from cc_feishu_bridge.claude.memory_manager import MemoryManager


@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "memories.db")
        m = MemoryManager(db_path)
        yield m


def test_add_preference_and_get_all(mgr):
    """user_preferences: 添加后能取回，字段正确"""
    pref = mgr.add_preference("ou_test", "主人信息", "我叫狗蛋，我的主人叫姚日华", "狗蛋,主人,姚日华")
    prefs = mgr.get_all_preferences()
    assert len(prefs) == 1
    assert prefs[0].id == pref.id
    assert prefs[0].title == "主人信息"
    assert prefs[0].content == "我叫狗蛋，我的主人叫姚日华"
    assert prefs[0].keywords == "狗蛋,主人,姚日华"
    assert prefs[0].user_open_id == "ou_test"


def test_project_memories_isolated_by_path(mgr):
    """不同 project_path 的记忆互不干扰"""
    mgr.add_project_memory("/proj-a", "用 pnpm", "不要用 npm，用 pnpm install", "pnpm,npm")
    mgr.add_project_memory("/proj-b", "用 yarn", "不要用 npm，用 yarn", "yarn,npm")
    results_a = mgr.search_project_memories("pnpm", project_path="/proj-a")
    results_b = mgr.search_project_memories("pnpm", project_path="/proj-b")
    assert any("pnpm" in r.memory.content for r in results_a)
    assert not any("pnpm" in r.memory.content for r in results_b)


def test_inject_context_returns_all_preferences(mgr):
    """inject_context 返回指定用户的 user_preferences"""
    mgr.add_preference("ou_test", "主人信息", "我叫狗蛋", "狗蛋")
    ctx = mgr.inject_context("ou_test")
    assert "主人信息" in ctx
    assert "我叫狗蛋" in ctx


def test_inject_context_format_correctly(mgr):
    """inject_context 格式正确：标题+内容"""
    mgr.add_preference("ou_test", "发版规则", "发版前必须确认", "发版,确认")
    mgr.add_preference("ou_test", "主人信息", "我叫狗蛋", "狗蛋")
    ctx = mgr.inject_context("ou_test")
    assert "【用户偏好】" in ctx
    assert "发版规则" in ctx
    assert "发版前必须确认" in ctx
    assert "主人信息" in ctx


def test_inject_context_empty_when_no_preferences(mgr):
    """无用户偏好时返回空字符串"""
    ctx = mgr.inject_context("ou_test")
    assert ctx == ""


def test_delete_project_memory(mgr):
    """删除项目记忆"""
    mem = mgr.add_project_memory("/proj", "测试记忆", "这是测试内容", "测试")
    results = mgr.search_project_memories("测试", project_path="/proj")
    assert len(results) == 1
    deleted = mgr.delete_project_memory(mem.id)
    assert deleted is not None
    assert deleted["title"] == "测试记忆"
    assert deleted["content"] == "这是测试内容"
    assert deleted["keywords"] == "测试"
    assert deleted["id"] == mem.id
    results_after = mgr.search_project_memories("测试", project_path="/proj")
    assert len(results_after) == 0


def test_clear_project_memories(mgr):
    """清空某项目下所有记忆"""
    mgr.add_project_memory("/proj", "记忆1", "内容1", "关键词1")
    mgr.add_project_memory("/proj", "记忆2", "内容2", "关键词2")
    mgr.add_project_memory("/other", "其他", "其他内容", "其他")
    count = mgr.clear_project_memories("/proj")
    assert count == 2
    results = mgr.search_project_memories("记忆", project_path="/proj")
    assert len(results) == 0
    results_other = mgr.search_project_memories("其他", project_path="/other")
    assert len(results_other) == 1


# ── Regression: QA session 2026-04-07 ─────────────────────────────────────────
# ISSUE: _handle_memory_user called add_preference(title, content, keywords)
# without user_open_id — missing required first argument.
# This tests the contract that user_open_id is preserved in all operations.

def test_update_preference_preserves_user_open_id(mgr):
    """update_preference invalidates the cache keyed by user_open_id"""
    mgr.add_preference("ou_alice", "标题A", "内容A", "a")
    mgr.add_preference("ou_alice", "标题B", "内容B", "b")
    # Cache is populated for ou_alice
    prefs1 = mgr.get_preferences_by_user("ou_alice")
    assert len(prefs1) == 2
    # Update one — cache must be invalidated
    ok = mgr.update_preference(prefs1[0].id, "标题A已改", "新内容A", "改")
    assert ok
    prefs2 = mgr.get_preferences_by_user("ou_alice")
    assert len(prefs2) == 2
    assert any(p.title == "标题A已改" for p in prefs2)
    assert any(p.content == "新内容A" for p in prefs2)


def test_delete_preference_preserves_user_open_id(mgr):
    """delete_preference invalidates the cache keyed by user_open_id"""
    mgr.add_preference("ou_bob", "记忆1", "内容1", "k1")
    mgr.add_preference("ou_bob", "记忆2", "内容2", "k2")
    # Populate cache
    prefs = mgr.get_preferences_by_user("ou_bob")
    assert len(prefs) == 2
    # Delete one — cache must be invalidated
    ok = mgr.delete_preference(prefs[0].id)
    assert ok
    prefs_after = mgr.get_preferences_by_user("ou_bob")
    assert len(prefs_after) == 1
    assert prefs_after[0].id == prefs[1].id


def test_prefs_cache_isolates_users(mgr):
    """Cache key includes user_open_id — different users do not share cache"""
    mgr.add_preference("ou_x", "X的记忆", "X内容", "x")
    mgr.add_preference("ou_y", "Y的记忆", "Y内容", "y")
    # Populate cache for ou_x
    prefs_x = mgr.get_preferences_by_user("ou_x")
    # Direct DB insert for ou_x (bypassing cache)
    mgr.add_preference("ou_x", "X的第三条", "X内容3", "x3")
    # Next call for ou_x must re-query DB (cache was invalidated on add)
    prefs_x2 = mgr.get_preferences_by_user("ou_x")
    assert any(p.title == "X的第三条" for p in prefs_x2)
    # ou_y cache is unaffected
    prefs_y = mgr.get_preferences_by_user("ou_y")
    assert len(prefs_y) == 1
    assert prefs_y[0].title == "Y的记忆"


def test_add_preference_requires_user_open_id(mgr):
    """Regression: add_preference(user_open_id, ...) — user_open_id must be stored"""
    pref = mgr.add_preference("ou_regression_test", "标题", "内容", "k")
    assert pref.user_open_id == "ou_regression_test"
    retrieved = mgr.get_preferences_by_user("ou_regression_test")
    assert len(retrieved) == 1
    assert retrieved[0].user_open_id == "ou_regression_test"

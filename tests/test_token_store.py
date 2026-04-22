import tempfile, os
from cc_feishu_bridge.feishu.token_store import UserTokenStore

def test_store_and_retrieve():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        store.save("ou_abc123", {
            "access_token": "at_xxx",
            "refresh_token": "rt_yyy",
            "expires_at": "2026-04-02T12:00:00Z",
        })
        token = store.load("ou_abc123")
        assert token["access_token"] == "at_xxx"
        assert token["refresh_token"] == "rt_yyy"

def test_load_missing_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        assert store.load("ou_unknown") is None

def test_remove_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        store.save("ou_del", {"access_token": "x"})
        store.remove("ou_del")
        assert store.load("ou_del") is None

def test_multiple_users():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserTokenStore(os.path.join(tmpdir, "user_tokens.yaml"))
        store.save("ou_1", {"access_token": "tok1"})
        store.save("ou_2", {"access_token": "tok2"})
        assert store.load("ou_1")["access_token"] == "tok1"
        assert store.load("ou_2")["access_token"] == "tok2"
        assert store.load("ou_3") is None
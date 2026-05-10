import pytest
from core.commands.new_session import NewSessionHandler
from core.protocol import SessionKey

@pytest.fixture
def handler():
    return NewSessionHandler()

@pytest.mark.asyncio
async def test_new_session(handler):
    key = SessionKey(bot_id="b", project_path="/p", platform="feishu", chat_id="c")
    result = await handler.execute("", {
        "session_key": key,
        "user_open_id": "u1",
        "config": None,
        "data_dir": "/tmp",
    })
    assert result.content
    assert "新会话" in result.content
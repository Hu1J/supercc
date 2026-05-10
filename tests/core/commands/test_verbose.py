import pytest
from core.commands.verbose import VerboseHandler

@pytest.fixture
def handler():
    return VerboseHandler()

@pytest.mark.asyncio
async def test_verbose_returns_content(handler):
    # /verbose without args shows current config
    result = await handler.execute("", {"platform": "feishu", "chat_id": "oc_test", "config": None})
    assert result.content
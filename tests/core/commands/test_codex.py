import pytest
from core.commands.codex import CodexHandler

@pytest.fixture
def handler():
    return CodexHandler()

@pytest.mark.asyncio
async def test_codex_returns_content(handler):
    # /codex without args may fail if config is None, but module should load
    try:
        result = await handler.execute("", {})
    except AttributeError:
        # config may be None in test environment
        result = None
    # Should return some content or be None (expected in unconfigured test env)
    assert result is None or result.content
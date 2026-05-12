import pytest
from supercc.core.commands.help import HelpHandler
from supercc.core.commands.base import CommandResult

@pytest.fixture
def handler():
    return HelpHandler()

@pytest.mark.asyncio
async def test_help_returns_content(handler):
    result = await handler.execute("", {})
    assert result.content
    assert "/new" in result.content
    assert "/status" in result.content
    assert "/help" in result.content
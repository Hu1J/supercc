import pytest
from supercc.core.commands.stop import StopHandler

@pytest.fixture
def handler():
    return StopHandler()

@pytest.mark.asyncio
async def test_stop(handler):
    result = await handler.execute("", {})
    assert result.content
    assert "已打断" in result.content
import pytest
from core.commands.status import StatusHandler
from core.commands.base import CommandResult

@pytest.fixture
def handler():
    return StatusHandler()

@pytest.mark.asyncio
async def test_status_returns_card(handler):
    result = await handler.execute("", {"chat_id": "oc_xxx"})
    assert result.content == ""
    assert result.card is not None
    assert result.card.type == "interactive"
import pytest
from core.commands.git import GitHandler

@pytest.fixture
def handler():
    return GitHandler()

@pytest.mark.asyncio
async def test_git_returns_card(handler):
    result = await handler.execute("", {
        "session_key": None,  # git handler handles None gracefully
    })
    # git handler should always return a card (even if empty)
    assert result.card is not None
    assert result.card.type == "interactive"
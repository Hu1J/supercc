import pytest
from supercc.core.commands.memory import MemoryHandler


@pytest.fixture
def handler():
    return MemoryHandler()


@pytest.mark.asyncio
async def test_memory_returns_help_text(handler):
    """No args returns help text."""
    result = await handler.execute("", {})
    assert result.content
    assert "/memory user" in result.content
    assert "/memory proj" in result.content


@pytest.mark.asyncio
async def test_memory_returns_command_result(handler):
    """Execute returns CommandResult with content."""
    result = await handler.execute("user list", {"user_open_id": "test_user"})
    assert result is not None
    assert hasattr(result, "content")
    # No preferences yet, should show empty message
    assert "暂无用户偏好" in result.content or "📭" in result.content
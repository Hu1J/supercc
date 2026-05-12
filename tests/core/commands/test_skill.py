import pytest
from supercc.core.commands.skill import SkillHandler


@pytest.fixture
def handler():
    return SkillHandler()


@pytest.mark.asyncio
async def test_skill_returns_command_result(handler):
    """Execute returns CommandResult with content."""
    result = await handler.execute("", {})
    assert result is not None
    assert hasattr(result, "content")


@pytest.mark.asyncio
async def test_skill_all_returns_command_result(handler):
    """Execute with 'all' arg returns CommandResult with content."""
    result = await handler.execute("all", {})
    assert result is not None
    assert hasattr(result, "content")
import pytest
import pytest_asyncio
from supercc.core.commands.router import CommandRouter

@pytest.mark.asyncio
async def test_router_dispatch_help():
    router = CommandRouter()
    result = await router.dispatch("help", "", {})
    assert result is not None
    assert "/new" in result.content
    assert "/status" in result.content

@pytest.mark.asyncio
async def test_router_unknown_command():
    router = CommandRouter()
    result = await router.dispatch("foobar", "", {})
    assert "未知命令" in result.content
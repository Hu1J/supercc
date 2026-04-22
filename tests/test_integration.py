import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def integration():
    from cc_feishu_bridge.claude.integration import ClaudeIntegration
    return ClaudeIntegration(cli_path="/bin/false", max_turns=10)


@pytest.mark.anyio
async def test_query_handles_missing_sdk(integration):
    with patch.dict("sys.modules", {"claude_agent_sdk": None}):
        with pytest.raises(RuntimeError, match="claude-agent-sdk is required"):
            await integration.query("hello")


def test_parse_message_text_block(integration):
    """_parse_message handles TextBlock from AssistantMessage."""
    class TextBlock:
        __name__ = "TextBlock"
        def __init__(self): self.text = "Hello "

    class AssistantMessage:
        __name__ = "AssistantMessage"
        def __init__(self, blocks): self.content = blocks

    msg_obj = AssistantMessage([TextBlock()])
    msg = integration._parse_message(msg_obj)
    assert msg is not None
    assert msg.content == "Hello "
    assert msg.is_final is False


def test_parse_message_tool_use_block(integration):
    """_parse_message handles ToolUseBlock from AssistantMessage."""
    class ToolUseBlock:
        __name__ = "ToolUseBlock"
        def __init__(self): self.name = "Read"; self.input = {"file_path": "main.py"}

    class AssistantMessage:
        __name__ = "AssistantMessage"
        def __init__(self, blocks): self.content = blocks

    msg_obj = AssistantMessage([ToolUseBlock()])
    msg = integration._parse_message(msg_obj)
    assert msg is not None
    assert msg.tool_name == "Read"
    assert "main.py" in msg.tool_input
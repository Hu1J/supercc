import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


def test_ws_client_initializes():
    from supercc.adapter.feishu.ws_client import FeishuWSClient
    client = FeishuWSClient(
        app_id="test_app_id",
        app_secret="test_secret",
        on_message=AsyncMock(),
    )
    assert client.app_id == "test_app_id"
    assert client.app_secret == "test_secret"
    assert client._handler is None  # not built until start() or _handle_p2p_message()
    assert client._ws_client is None  # not started yet


def test_on_message_callback():
    from supercc.adapter.feishu.ws_client import FeishuWSClient
    cb = AsyncMock()
    client = FeishuWSClient(app_id="id", app_secret="secret", on_message=cb)

    # Mock event object simulating lark event
    mock_event = MagicMock()
    mock_event.event.message.message_id = "msg_123"
    mock_event.event.message.chat_id = "chat_abc"
    mock_event.event.message.msg_type = "text"
    mock_event.event.message.content = '{"text":"hello"}'
    mock_event.event.message.create_time = "1234567890"
    mock_event.event.message.parent_id = "om_parent_456"
    mock_event.event.message.thread_id = "om_thread_789"

    mock_sender = MagicMock()
    mock_sender.sender_id.open_id = "user_xyz"
    mock_event.event.sender = mock_sender

    # Run handler + yield in a proper event loop to avoid "no current event loop" error
    async def run_test():
        client._handle_p2p_message(mock_event)
        # Give the scheduled callback a chance to run
        await asyncio.sleep(0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_test())
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    cb.assert_called_once()
    msg = cb.call_args[0][0]
    assert msg.message_id == "msg_123"
    assert msg.chat_id == "chat_abc"
    assert msg.user_open_id == "user_xyz"
    assert msg.content == "hello"
    assert msg.parent_id == "om_parent_456"
    assert msg.thread_id == "om_thread_789"

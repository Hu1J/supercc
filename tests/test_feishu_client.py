import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from supercc.adapter.feishu.client import FeishuClient, IncomingMessage


def test_parse_incoming_text_message():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    body = {
        "event": {
            "message": {
                "message_id": "om_123",
                "chat_id": "oc_456",
                "msg_type": "text",
                "content": '{"text": "hello world"}',
                "create_time": "1234567890",
                "parent_id": "om_parent_123",
                "thread_id": "om_thread_456",
            },
            "sender": {
                "sender_id": {"open_id": "ou_789"},
            },
        }
    }
    msg = client.parse_incoming_message(body)
    assert msg is not None
    assert msg.message_id == "om_123"
    assert msg.content == "hello world"
    assert msg.user_open_id == "ou_789"
    assert msg.parent_id == "om_parent_123"
    assert msg.thread_id == "om_thread_456"


def test_parse_incoming_empty_body():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    msg = client.parse_incoming_message({})
    assert msg is None


def test_parse_non_text_message():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    body = {
        "event": {
            "message": {
                "message_id": "om_123",
                "chat_id": "oc_456",
                "msg_type": "image",
                "content": '{"file_key": "img_xxx"}',
                "create_time": "1234567890",
            },
            "sender": {
                "sender_id": {"open_id": "ou_789"},
            },
        }
    }
    msg = client.parse_incoming_message(body)
    assert msg is not None
    assert msg.message_type == "image"


def test_client_accepts_data_dir():
    client = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="/tmp/test")
    assert client.data_dir == "/tmp/test"


def test_client_data_dir_defaults_to_empty():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    assert client.data_dir == ""


def test_extract_file_info():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    name, ftype = client._extract_file_info('{"file_name": "report", "file_type": "pdf"}')
    assert name == "report"
    assert ftype == "pdf"


def test_extract_file_info_invalid_json():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    name, ftype = client._extract_file_info("not json")
    assert name == "file"
    assert ftype == "bin"


def test_get_message_success():
    """get_message() should return message dict when API succeeds."""
    client = FeishuClient(app_id="cli_test", app_secret="secret")

    # Simulate lark SDK Message object with msg_type, body.content, sender.id
    mock_body = MagicMock()
    mock_body.content = '{"text":"hello"}'
    mock_sender = MagicMock()
    mock_sender.id = "ou_123"
    mock_item = MagicMock()
    mock_item.msg_type = "text"
    mock_item.body = mock_body
    mock_item.sender = mock_sender

    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data.items = [mock_item]

    with patch.object(client, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        with patch('asyncio.to_thread', return_value=mock_response):
            result = asyncio.run(client.get_message("om_123"))
    assert result is not None
    assert result["msg_type"] == "text"
    assert result["content"] == '{"text":"hello"}'
    assert result["sender_id"] == "ou_123"


def test_get_message_failure_returns_none():
    """get_message() should return None when API call fails."""
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    mock_response = MagicMock()
    mock_response.success.return_value = False
    mock_response.msg = "not found"

    with patch.object(client, '_get_client') as mock_get_client:
        mock_get_client.return_value = MagicMock()
        with patch('asyncio.to_thread', return_value=mock_response):
            result = asyncio.run(client.get_message("om_bad"))
    assert result is None


def test_send_text_reply():
    """send_text_reply() should use ReplyMessageRequest and return message_id."""
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    mock_response = MagicMock()
    mock_response.success.return_value = True
    mock_response.data.message_id = "om_reply_123"

    with patch.object(client, '_get_client') as mock_get_client:
        mock_get_client.return_value = MagicMock()
        with patch('asyncio.to_thread', return_value=mock_response):
            result = asyncio.run(client.send_text_reply("chat_abc", "hello", "om_original"))
    assert result == "om_reply_123"

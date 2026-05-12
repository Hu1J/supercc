"""Tests for core/protocol.py."""

import pytest
from datetime import datetime

from supercc.core.protocol import (
    SessionKey,
    InboundMessage,
    OutboundMessage,
    ToolCall,
    ToolResult,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcError,
    MessageRole,
    MessageType,
    Event,
    ErrorCode,
)


class TestSessionKey:
    def test_four_key_tuple(self):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        assert key.bot_id == "bot_123"
        assert key.project_path == "/Users/test/project"
        assert key.platform == "feishu"
        assert key.chat_id == "oc_abc123"

    def test_str_representation(self):
        key = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        s = str(key)
        assert "bot_123" in s
        assert "feishu" in s
        assert "oc_abc123" in s

    def test_equality_and_hash(self):
        key1 = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        key2 = SessionKey(
            bot_id="bot_123",
            project_path="/Users/test/project",
            platform="feishu",
            chat_id="oc_abc123",
        )
        assert key1 == key2
        # Test hashability (used in dict/set)
        d = {key1: "value"}
        assert d[key2] == "value"


class TestJsonRpcFrames:
    def test_request_to_dict(self):
        req = JsonRpcRequest(
            id=1,
            method="core.subscribe",
            params={"keys": [{"bot_id": "bot_1", "project_path": "/p", "platform": "feishu", "chat_id": "oc_1"}]},
        )
        d = req.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["method"] == "core.subscribe"
        assert "keys" in d["params"]

    def test_request_from_dict(self):
        raw = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "message",
            "params": {"content": "hello"},
        }
        req = JsonRpcRequest.from_dict(raw)
        assert req.jsonrpc == "2.0"
        assert req.id == 42
        assert req.method == "message"
        assert req.params["content"] == "hello"

    def test_response_success(self):
        resp = JsonRpcResponse(id=1, result={"status": "ok"})
        d = resp.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["result"]["status"] == "ok"
        assert "error" not in d

    def test_response_error(self):
        resp = JsonRpcResponse(
            id=1,
            error=JsonRpcError(code=1001, message="Session not found"),
        )
        d = resp.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["error"]["code"] == 1001
        assert d["error"]["message"] == "Session not found"

    def test_error_to_dict(self):
        err = JsonRpcError(code=ErrorCode.METHOD_NOT_FOUND, message="method not found", data={"hint": "check name"})
        d = err.to_dict()
        assert d["code"] == -32601
        assert d["message"] == "method not found"
        assert d["data"]["hint"] == "check name"


class TestMessageRole:
    def test_role_values(self):
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.SYSTEM == "system"


class TestMessageType:
    def test_type_values(self):
        assert MessageType.TEXT == "text"
        assert MessageType.IMAGE == "image"
        assert MessageType.FILE == "file"
        assert MessageType.TOOL_CALL == "tool_call"
        assert MessageType.TOOL_RESULT == "tool_result"


class TestEvent:
    def test_inbound_events(self):
        assert Event.MESSAGE == "message"
        assert Event.STREAM_CHUNK == "stream_chunk"
        assert Event.TOOL_RESULT == "tool_result"
        assert Event.PING == "ping"

    def test_outbound_events(self):
        assert Event.RESPONSE == "response"
        assert Event.TOOL_CALL == "tool_call"
        assert Event.ERROR == "error"
        assert Event.PONG == "pong"


def test_command_result_types():
    from supercc.core.protocol import CommandResult, CommandCard
    # text result
    r = CommandResult(content="hello")
    assert r.content == "hello"
    assert r.card is None
    # card result
    card = CommandCard(type="markdown", data={"elements": []})
    r2 = CommandResult(content="", card=card)
    assert r2.card is not None

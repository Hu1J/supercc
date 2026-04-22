# tests/claude/test_feishu_file_tools.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile
import os
from unittest.mock import AsyncMock, patch, MagicMock


def test_guess_file_type_image():
    """图片文件被正确识别为图片类型"""
    from cc_feishu_bridge.feishu.media import guess_file_type
    assert guess_file_type(".png") == "png"
    assert guess_file_type(".jpg") == "png"
    assert guess_file_type(".gif") == "gif"
    assert guess_file_type(".webp") == "webp"


def test_guess_file_type_doc():
    """文档文件被识别为对应类型"""
    from cc_feishu_bridge.feishu.media import guess_file_type
    assert guess_file_type(".pdf") == "pdf"
    assert guess_file_type(".docx") == "doc"
    assert guess_file_type(".xlsx") == "xls"


def test_guess_file_type_stream():
    """未知类型默认 stream"""
    from cc_feishu_bridge.feishu.media import guess_file_type
    assert guess_file_type(".xyz") == "stream"


def _make_capture_server_side_effect():
    """Return a side_effect that wraps the real tool handler for mocking."""
    def capture_server(**kwargs):
        real_tool = kwargs["tools"][0]
        mock_tool = MagicMock()
        mock_tool.fn = real_tool.handler
        return MagicMock(tools=[mock_tool])
    return capture_server


def test_feishu_send_file_no_files():
    """未提供文件时返回错误"""
    # Clear cached modules so re-import picks up patches
    for key in list(sys.modules.keys()):
        if "cc_feishu_bridge" in key or "claude_agent_sdk" in key:
            del sys.modules[key]

    with patch("claude_agent_sdk.create_sdk_mcp_server") as mock_create, \
         patch("cc_feishu_bridge.claude.feishu_file_tools._get_chat_id", return_value=None):
        from cc_feishu_bridge.claude import feishu_file_tools
        feishu_file_tools._mcp_server = None
        mock_create.side_effect = _make_capture_server_side_effect()

        import asyncio
        server = feishu_file_tools.get_feishu_file_mcp_server()
        tool_fn = server.tools[0]
        result = asyncio.get_event_loop().run_until_complete(
            tool_fn.fn({"file_paths": []})
        )
        assert result["is_error"] is True
        assert "未提供文件路径" in result["content"][0]["text"]


def test_feishu_send_file_no_chat_id():
    """无活跃会话时返回错误"""
    for key in list(sys.modules.keys()):
        if "cc_feishu_bridge" in key or "claude_agent_sdk" in key:
            del sys.modules[key]

    with patch("claude_agent_sdk.create_sdk_mcp_server") as mock_create, \
         patch("cc_feishu_bridge.claude.feishu_file_tools._get_chat_id", return_value=None):
        from cc_feishu_bridge.claude import feishu_file_tools
        feishu_file_tools._mcp_server = None
        mock_create.side_effect = _make_capture_server_side_effect()

        import asyncio
        server = feishu_file_tools.get_feishu_file_mcp_server()
        tool_fn = server.tools[0]
        result = asyncio.get_event_loop().run_until_complete(
            tool_fn.fn({"file_paths": ["/tmp/nonexistent.png"]})
        )
        assert result["is_error"] is True
        assert "未找到活跃飞书会话" in result["content"][0]["text"]

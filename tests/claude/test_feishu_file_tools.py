# tests/claude/test_feishu_file_tools.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import asyncio
from unittest.mock import patch


def test_guess_file_type_image():
    """图片文件被正确识别为图片类型"""
    from supercc.adapter.feishu.media import guess_file_type
    assert guess_file_type(".png") == "png"
    assert guess_file_type(".jpg") == "png"
    assert guess_file_type(".gif") == "gif"
    assert guess_file_type(".webp") == "webp"


def test_guess_file_type_doc():
    """文档文件被识别为对应类型"""
    from supercc.adapter.feishu.media import guess_file_type
    assert guess_file_type(".pdf") == "pdf"
    assert guess_file_type(".docx") == "doc"
    assert guess_file_type(".xlsx") == "xls"


def test_guess_file_type_stream():
    """未知类型默认 stream"""
    from supercc.adapter.feishu.media import guess_file_type
    assert guess_file_type(".xyz") == "stream"


def test_feishu_send_file_no_files():
    """未提供文件时返回错误"""
    from supercc.claude.feishu_file_tools import feishu_send_file
    result = asyncio.get_event_loop().run_until_complete(
        feishu_send_file.handler({"file_paths": []})
    )
    assert result["is_error"] is True
    assert "未提供文件路径" in result["content"][0]["text"]


def test_feishu_send_file_no_chat_id():
    """无活跃会话时返回错误"""
    from supercc.claude.feishu_file_tools import feishu_send_file
    with patch("supercc.claude.feishu_file_tools._get_chat_id", return_value=None):
        result = asyncio.get_event_loop().run_until_complete(
            feishu_send_file.handler({"file_paths": ["/tmp/nonexistent.png"]})
        )
    assert result["is_error"] is True
    assert "未找到活跃飞书会话" in result["content"][0]["text"]

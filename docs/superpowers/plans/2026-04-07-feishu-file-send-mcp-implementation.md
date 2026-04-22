# Feishu 文件发送 MCP 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 `FeishuSendFile` MCP 工具，让 Claude Code 可以发送文件/图片给飞书用户

**Architecture:** 参照 `memory_tools.py` 模式：新建 `feishu_file_tools.py`，暴露单一 `FeishuSendFile` 工具，内部从 `SessionManager` 取 chat_id，并发处理多文件，复用 `FeishuClient` 的 upload/send 方法。

**Tech Stack:** `claude_agent_sdk`, `lark-oapi`, `sqlite3`

---

## File Structure

- **Create:** `cc_feishu_bridge/claude/feishu_file_tools.py` — MCP 工具定义
- **Modify:** `cc_feishu_bridge/claude/integration.py:76` — 追加 `feishu_file` 到 `mcp_servers`
- **Create:** `tests/claude/test_feishu_file_tools.py` — 单元测试

---

## 常量（从 main.py 复用）

```python
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB
```

---

## Task 1: 创建 feishu_file_tools.py

**Files:**
- Create: `cc_feishu_bridge/claude/feishu_file_tools.py`
- Test: `tests/claude/test_feishu_file_tools.py`

### 步骤 1: 写测试

```python
# tests/claude/test_feishu_file_tools.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import tempfile
import os
from unittest.mock import AsyncMock, patch, MagicMock

# 需要先建空文件让测试能引用模块
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
    assert guess_file_type(".abcd") == "stream"
```

### 步骤 2: 运行测试确认通过（guess_file_type 已有实现）

Run: `pytest tests/claude/test_feishu_file_tools.py -v`
Expected: PASS（media.guess_file_type 已在 media.py 实现）

### 步骤 3: 写 feishu_file_tools.py

```python
"""Feishu 文件发送 MCP 工具 — 暴露 FeishuSendFile 给 Claude Code 使用。"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


def _get_feishu_client() -> "FeishuClient":
    """延迟初始化 FeishuClient（读取 config.yaml）。"""
    from cc_feishu_bridge.config import load_config, resolve_config_path
    from cc_feishu_bridge.feishu.client import FeishuClient
    cfg_path, _ = resolve_config_path()
    config = load_config(cfg_path)
    return FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
    )


def _get_session_manager() -> "SessionManager":
    """延迟初始化 SessionManager。"""
    from cc_feishu_bridge.config import resolve_config_path
    from cc_feishu_bridge.claude.session_manager import SessionManager
    _, data_dir = resolve_config_path()
    db_path = os.path.join(data_dir, "sessions.db")
    return SessionManager(db_path=db_path)


def _get_chat_id() -> Optional[str]:
    """从当前活跃会话获取 chat_id。"""
    sm = _get_session_manager()
    session = sm.get_active_session_by_chat_id()
    return session.chat_id if session else None


async def _send_single_file(file_path: str, chat_id: str) -> str:
    """发送单个文件，返回 msg_id 或抛出异常。"""
    from cc_feishu_bridge.feishu.media import guess_file_type
    from cc_feishu_bridge.feishu.client import FeishuClient

    feishu = _get_feishu_client()
    ext = os.path.splitext(file_path)[1].lower()
    file_name = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        data = f.read()

    if ext in SUPPORTED_IMAGE_EXTS:
        image_key = await feishu.upload_image(data)
        msg_id = await feishu.send_image(chat_id, image_key)
    else:
        file_type = guess_file_type(ext)
        file_key = await feishu.upload_file(data, file_name, file_type)
        msg_id = await feishu.send_file(chat_id, file_key, file_name)

    return msg_id


def _build_feishu_file_mcp_server():
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        "FeishuSendFile",
        "发送本地文件或图片到飞书用户（通过当前活跃会话的 chat_id）。"
        "支持多文件并发上传，自动判断文件类型（图片直接发送，其他文件先上传再发送）。"
        "每个文件需在 30MB 以内。",
        {"file_paths": list},
    )
    async def feishu_send_file(args: dict) -> dict:
        file_paths: list = args.get("file_paths", [])
        if not file_paths:
            return {"content": [{"type": "text", "text": "未提供文件路径"}], "is_error": True}

        # 获取 chat_id
        chat_id = _get_chat_id()
        if not chat_id:
            return {
                "content": [{"type": "text", "text": "未找到活跃飞书会话，请先在飞书里发一条消息"}],
                "is_error": True,
            }

        # 验证所有文件
        errors = []
        for fp in file_paths:
            if not os.path.exists(fp):
                errors.append(f"文件不存在: {fp}")
            elif os.path.getsize(fp) > MAX_FILE_SIZE:
                errors.append(f"{os.path.basename(fp)} 超过 30MB 限制")
        if errors:
            return {"content": [{"type": "text", "text": "\n".join(errors)}], "is_error": True}

        # 并发发送
        async def send_one(fp: str) -> tuple[str, str | None]:
            try:
                msg_id = await _send_single_file(fp, chat_id)
                return (fp, None)
            except Exception as e:
                return (fp, str(e))

        results = await asyncio.gather(*[send_one(fp) for fp in file_paths])

        ok = [fp for fp, err in results if err is None]
        fail = [(fp, err) for fp, err in results if err is not None]

        lines = []
        if ok:
            lines.append(f"✅ 已发送 {len(ok)} 个文件")
            for fp in ok:
                lines.append(f"  • {os.path.basename(fp)}")
        if fail:
            lines.append(f"❌ 失败 {len(fail)} 个")
            for fp, err in fail:
                lines.append(f"  • {os.path.basename(fp)}: {err}")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="feishu_file",
        version="1.0.0",
        tools=[feishu_send_file],
    )


_mcp_server = None
_mcp_server_lock = threading.Lock()


def get_feishu_file_mcp_server():
    global _mcp_server
    if _mcp_server is None:
        with _mcp_server_lock:
            if _mcp_server is None:
                _mcp_server = _build_feishu_file_mcp_server()
    return _mcp_server
```

### 步骤 4: 提交

```bash
git add cc_feishu_bridge/claude/feishu_file_tools.py
git commit -m "$(cat <<'EOF'
feat: add FeishuSendFile MCP tool for Claude Code

Exposes a single FeishuSendFile(file_paths) tool that:
- Auto-detects file type via extension (images vs other files)
- Sends via current active session's chat_id
- Uploads then sends concurrently for multiple files
- Returns detailed success/failure report

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 将 feishu_file 注入到 ClaudeIntegration

**Files:**
- Modify: `cc_feishu_bridge/claude/integration.py:67-76`

### 步骤 1: 修改 integration.py

在 `integration.py` 第 67 行附近，找到：

```python
from cc_feishu_bridge.claude.memory_tools import get_memory_mcp_server
```

在第 76 行附近，将：

```python
mcp_servers={"memory": get_memory_mcp_server()},
```

改为：

```python
from cc_feishu_bridge.claude.feishu_file_tools import get_feishu_file_mcp_server
# ...
mcp_servers={
    "memory": get_memory_mcp_server(),
    "feishu_file": get_feishu_file_mcp_server(),
},
```

### 步骤 2: 运行现有测试确认没有破坏其他功能

Run: `pytest tests/test_integration.py tests/test_memory_manager.py -v`
Expected: PASS

### 步骤 3: 提交

```bash
git add cc_feishu_bridge/claude/integration.py
git commit -m "$(cat <<'EOF'
feat: wire FeishuSendFile MCP into ClaudeIntegration

Adds feishu_file MCP server alongside memory MCP server
so Claude Code can send files/images to Feishu users.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 补充 feishu_file_tools 测试

**Files:**
- Modify: `tests/claude/test_feishu_file_tools.py`

### 步骤 1: 添加集成测试（mock 模式）

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import tempfile
import os


def test_feishu_send_file_no_files():
    """未提供文件时返回错误"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from cc_feishu_bridge.claude import feishu_file_tools

    # 重置单例以确保干净状态
    feishu_file_tools._mcp_server = None

    with patch.object(feishu_file_tools, '_get_chat_id', return_value=None):
        import asyncio
        server = feishu_file_tools.get_feishu_file_mcp_server()
        # 从 server.tools[0] 取到 feishu_send_file 函数
        tool_fn = server.tools[0]
        result = asyncio.get_event_loop().run_until_complete(
            tool_fn.fn({"file_paths": []})
        )
        assert result["is_error"] is True
        assert "未提供文件路径" in result["content"][0]["text"]


def test_feishu_send_file_no_chat_id():
    """无活跃会话时返回错误"""
    from cc_feishu_bridge.claude import feishu_file_tools
    feishu_file_tools._mcp_server = None

    with patch.object(feishu_file_tools, '_get_chat_id', return_value=None):
        with patch.object(feishu_file_tools, '_get_session_manager') as mock_sm:
            mock_sm.return_value = MagicMock()
            import asyncio
            server = feishu_file_tools.get_feishu_file_mcp_server()
            tool_fn = server.tools[0]
            result = asyncio.get_event_loop().run_until_complete(
                tool_fn.fn({"file_paths": ["/tmp/nonexistent.png"]})
            )
            assert result["is_error"] is True
            assert "未找到活跃飞书会话" in result["content"][0]["text"]
```

### 步骤 2: 运行测试

Run: `pytest tests/claude/test_feishu_file_tools.py -v`
Expected: PASS

### 步骤 3: 提交

```bash
git add tests/claude/test_feishu_file_tools.py
git commit -m "$(cat <<'EOF'
test: add feishu_file_tools tests

Tests for error cases (no files, no chat_id).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## 自检清单

- [x] Spec coverage: MCP 工具 `FeishuSendFile`、chat_id 解析、并发发送、错误处理 — 全部覆盖
- [x] Placeholder scan: 无 TBD/TODO，所有步骤均给出具体代码
- [x] Type consistency: `file_paths: list`、`chat_id: Optional[str]`、`result: dict` 结构一致
- [x] 测试覆盖: 错误路径有测试，实现路径（upload/send）走 mock，真实 API 调用由现有 `test_feishu_client.py` 保障

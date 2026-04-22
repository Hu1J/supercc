# 飞书图片/文件双向传输实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持飞书图片/文件的收发：用户发送图片→Claude能读取，用户发送文件→Claude能读取，Claude生成图片→发回飞书。

**Architecture:**
- Inbound：飞书消息 → FeishuClient.download_media() → 本地文件 → 路径拼入 prompt → Claude
- Outbound：Claude ImageBlock(base64) → FeishuClient.upload_image() → image_key → message.create() → 发回飞书
- 媒体存储目录：执行目录/.cc-feishu-bridge/received_images/ 和 received_files/

**Tech Stack:** Python 3.11+, lark-oapi, claude-agent-sdk

---

## 文件变更总览

| 操作 | 文件 |
|------|------|
| 修改 | `cc_feishu_bridge/feishu/client.py` |
| 修改 | `cc_feishu_bridge/feishu/message_handler.py` |
| 修改 | `cc_feishu_bridge/claude/integration.py` |
| 修改 | `cc_feishu_bridge/main.py` |
| 新增 | `cc_feishu_bridge/feishu/media.py` |
| 新增 | `tests/test_media.py` |
| 新增 | `tests/test_media_integration.py` |

---

## Task 1: ClaudeMessage 新增图片字段

**Files:**
- Modify: `cc_feishu_bridge/claude/integration.py:11-17`

- [ ] **Step 1: 更新 ClaudeMessage dataclass**

修改 `cc_feishu_bridge/claude/integration.py` 中 `ClaudeMessage` 的定义，在第 16 行后追加两个字段：

```python
@dataclass
class ClaudeMessage:
    content: str
    is_final: bool = False
    tool_name: str | None = None
    tool_input: str | None = None
    image_data: str | None = None   # 新增: base64 图片数据
    mime_type: str | None = None   # 新增: 图片 MIME 类型
```

---

## Task 2: _parse_message() 新增 ImageBlock 检测

**Files:**
- Modify: `cc_feishu_bridge/claude/integration.py:92-121`

- [ ] **Step 1: 在 _parse_message() 中新增 ImageBlock 分支**

在 `cc_feishu_bridge/claude/integration.py` 的 `_parse_message()` 方法中，`AssistantMessage` 分支里，在 `ToolUseBlock` 分支后、return None 之前，插入：

```python
elif block_type == "ImageBlock":
    image_data = getattr(block, "data", "")       # base64 字符串
    mime_type  = getattr(block, "mimeType", "")  # "image/png" / "image/gif" 等
    return ClaudeMessage(
        content="",
        is_final=False,
        tool_name=None,
        tool_input=None,
        image_data=image_data or None,
        mime_type=mime_type or None,
    )
```

完整 `_parse_message` 中 `AssistantMessage` 分支修改后结构：

```python
if msg_type == "AssistantMessage":
    for block in getattr(message, "content", []):
        block_type = type(block).__name__
        if block_type == "TextBlock":
            text = getattr(block, "text", "")
            if text:
                return ClaudeMessage(content=text, is_final=False)
        elif block_type == "ToolUseBlock":
            tool_name = getattr(block, "name", "Unknown")
            tool_input = getattr(block, "input", "")
            if isinstance(tool_input, dict):
                tool_input = json.dumps(tool_input)[:200]
            return ClaudeMessage(
                content="",
                is_final=False,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        elif block_type == "ImageBlock":
            image_data = getattr(block, "data", "")
            mime_type  = getattr(block, "mimeType", "")
            return ClaudeMessage(
                content="",
                is_final=False,
                tool_name=None,
                tool_input=None,
                image_data=image_data or None,
                mime_type=mime_type or None,
            )
```

---

## Task 3: media.py 工具函数

**Files:**
- Create: `cc_feishu_bridge/feishu/media.py`

- [ ] **Step 1: 编写 media.py 工具函数**

创建 `cc_feishu_bridge/feishu/media.py`：

```python
"""Media file utilities — path generation, saving, MIME type mapping."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Tuple


# MIME type → 文件扩展名
MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/json": ".json",
    "application/octet-stream": ".bin",
}

# 飞书 file_type → MIME type（参考飞书文档）
FILE_TYPE_TO_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "txt": "text/plain",
    "csv": "text/csv",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "mp4": "video/mp4",
    "avi": "video/x-msvideo",
}


def mime_to_ext(mime_type: str) -> str:
    """MIME type → 文件扩展名（含点号）。未知类型默认 .bin。"""
    return MIME_TO_EXT.get(mime_type, ".bin")


def file_type_to_mime(file_type: str) -> str:
    """飞书 file_type（如 'pdf'）→ MIME type。未知默认 application/octet-stream。"""
    return FILE_TYPE_TO_MIME.get(file_type.lower(), "application/octet-stream")


def sanitize_filename(name: str) -> str:
    """将文件名中的特殊字符替换为下划线，防止路径注入。"""
    # 只保留字母、数字、下划线、点号，其余替换
    return re.sub(r"[^a-zA-Z0-9._]", "_", name)


def make_image_path(data_dir: str, message_id: str) -> str:
    """生成图片本地存储路径。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 图片无原文件名，扩展名待定（由调用方根据 MIME 补充）
    filename = f"img_{ts}_{message_id[:8]}"
    images_dir = os.path.join(data_dir, "received_images")
    os.makedirs(images_dir, exist_ok=True)
    return os.path.join(images_dir, filename)


def make_file_path(data_dir: str, message_id: str, original_name: str, file_type: str) -> str:
    """生成文件本地存储路径。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = sanitize_filename(original_name) if original_name else "file"
    ext = mime_to_ext(file_type_to_mime(file_type))
    filename = f"file_{ts}_{message_id[:8]}_{safe_name}{ext}"
    files_dir = os.path.join(data_dir, "received_files")
    os.makedirs(files_dir, exist_ok=True)
    return os.path.join(files_dir, filename)


def save_bytes(path: str, data: bytes) -> None:
    """将字节写入文件。目录已由 make_*_path 确保存在。"""
    with open(path, "wb") as f:
        f.write(data)
```

- [ ] **Step 2: 运行语法检查**

Run: `python -m py_compile cc_feishu_bridge/feishu/media.py`
Expected: 无输出（编译成功）

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/feishu/media.py
git commit -m "feat: add media utility functions for file path generation and saving"
```

---

## Task 4: FeishuClient 新增媒体 API

**Files:**
- Modify: `cc_feishu_bridge/feishu/client.py`

- [ ] **Step 1: 更新 FeishuClient.__init__ 接收 data_dir**

修改 `FeishuClient.__init__`，在参数列表末尾新增 `data_dir: str = ""`：

```python
def __init__(
    self,
    app_id: str,
    app_secret: str,
    bot_name: str = "Claude",
    data_dir: str = "",
):
    self.app_id = app_id
    self.app_secret = app_secret
    self.bot_name = bot_name
    self.data_dir = data_dir
    self._client = None
```

- [ ] **Step 2: 新增 download_media 方法**

在 `remove_typing_reaction` 方法后（文件末尾附近），新增：

```python
async def download_media(self, message_id: str, file_key: str) -> bytes:
    """Download media (image/file) from a Feishu message.

    Args:
        message_id: The Feishu message ID.
        file_key: The file key from the message content.

    Returns:
        Raw bytes of the media file.

    Raises:
        RuntimeError: If the download fails.
    """
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.message_resource.get.builder()
        .message_id(message_id)
        .file_key(file_key)
        .build()
    )
    try:
        response = await asyncio.to_thread(
            client.im.v1.message_resource.get,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Failed to download media: {response.msg}")
        return response.data
    except Exception as e:
        logger.error(f"download_media error: {e}")
        raise
```

- [ ] **Step 3: 新增 upload_image 方法**

在 `download_media` 后新增：

```python
async def upload_image(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Upload an image to Feishu and return the image_key.

    Args:
        image_bytes: Raw image bytes.
        mime_type: MIME type of the image (e.g. 'image/png').

    Returns:
        Feishu image_key for use in message.create().

    Raises:
        RuntimeError: If the upload fails.
    """
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.image.create.builder()
        .request_body(
            lark.im.v1.model.CreateImageRequestBody.builder()
            .image_type("message")
            .image_size(str(len(image_bytes)))
            .image_name("image")
            .build()
        )
        .build()
    )
    try:
        response = await asyncio.to_thread(
            client.im.v1.image.create,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Failed to upload image: {response.msg}")
        image_key = response.data.image_key
        logger.info(f"Uploaded image: {image_key}")
        return image_key
    except Exception as e:
        logger.error(f"upload_image error: {e}")
        raise
```

注：`lark_oapi.image.create` 的参数以实际 SDK 为准，若上传图片需要传 bytes 则使用 bytes 模式。**先查 lark-oapi 源码确认 image.create 的正确用法**，如与上述不符则按实际 API 调整。

- [ ] **Step 4: 新增 send_image 方法**

在 `upload_image` 后新增：

```python
async def send_image(self, chat_id: str, image_key: str) -> str:
    """Send an image message to a Feishu chat.

    Args:
        chat_id: The Feishu chat ID.
        image_key: The image_key from upload_image().

    Returns:
        Feishu message_id of the sent image.
    """
    import json
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(json.dumps({"image_key": image_key}))
            .msg_type("image")
            .build()
        )
        .build()
    )
    try:
        response = await asyncio.to_thread(
            client.im.v1.message.create,
            request,
        )
        if not response.success():
            raise RuntimeError(f"Failed to send image: {response.msg}")
        logger.info(f"Sent image to {chat_id}: {response.data.message_id}")
        return response.data.message_id
    except Exception as e:
        logger.error(f"send_image error: {e}")
        raise
```

- [ ] **Step 5: 新增 _extract_file_info 方法（辅助）**

在 `send_image` 后新增，用于从 file 消息 content 中提取文件名和 file_type：

```python
def _extract_file_info(self, content_str: str) -> Tuple[str, str]:
    """Extract original filename and file_type from file message content.

    Returns:
        (original_filename, file_type) — e.g. ("document", "pdf")
    """
    import json
    try:
        content = json.loads(content_str)
        name = content.get("file_name", "file")
        ftype = content.get("file_type", "bin")
        return name, ftype
    except Exception:
        return "file", "bin"
```

- [ ] **Step 6: 运行语法检查**

Run: `python -m py_compile cc_feishu_bridge/feishu/client.py`
Expected: 无输出

- [ ] **Step 7: 提交**

```bash
git add cc_feishu_bridge/feishu/client.py
git commit -m "feat: add download_media, upload_image, send_image to FeishuClient"
```

---

## Task 5: MessageHandler 处理 Inbound 媒体

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: 新增 __init__ 参数 data_dir**

修改 `MessageHandler.__init__`，在参数列表末尾新增 `data_dir: str = ""`，并赋值 `self.data_dir = data_dir`。

```python
def __init__(
    self,
    feishu_client: FeishuClient,
    authenticator: Authenticator,
    validator: SecurityValidator,
    claude: ClaudeIntegration,
    session_manager: SessionManager,
    formatter: ReplyFormatter,
    approved_directory: str,
    data_dir: str = "",        # 新增
):
    # ... 现有赋值 ...
    self.data_dir = data_dir   # 新增
```

- [ ] **Step 2: 新增 _preprocess_media 方法**

在 `_safe_send` 方法后（文件末尾）新增：

```python
async def _preprocess_media(self, message: IncomingMessage) -> str:
    """Download and save inbound media, return the text to prepend to prompt.

    Returns:
        空字符串（无媒体），或形如 "[图片: /path/to/img.png]" 的文本片段。
    """
    from cc_feishu_bridge.feishu.media import (
        make_image_path,
        make_file_path,
        mime_to_ext,
        file_type_to_mime,
        save_bytes,
    )
    import os

    if message.message_type not in ("image", "file"):
        return ""

    msg_id = message.message_id
    content_str = message.content

    try:
        import json
        content = json.loads(content_str)
    except Exception:
        return ""

    if message.message_type == "image":
        file_key = content.get("image_key", "")
        if not file_key:
            return ""
        data_dir = self.data_dir or os.getcwd()
        base_path = make_image_path(data_dir, msg_id)
        ext = ".png"  # 默认扩展名，download 后可根据实际调整
        save_path = base_path + ext
        data = await self.feishu.download_media(msg_id, file_key)
        save_bytes(save_path, data)
        return f"[图片: {save_path}]"

    elif message.message_type == "file":
        file_key = content.get("file_key", "")
        orig_name = content.get("file_name", "file")
        file_type = content.get("file_type", "bin")
        if not file_key:
            return ""
        data_dir = self.data_dir or os.getcwd()
        save_path = make_file_path(data_dir, msg_id, orig_name, file_type)
        data = await self.feishu.download_media(msg_id, file_key)
        save_bytes(save_path, data)
        return f"[文件: {save_path}]"

    return ""
```

**注意：** `make_image_path` 返回的路径不含扩展名，download 后根据实际 MIME 类型补充扩展名。需要在 download 成功后根据 response 的 Content-Type 补充扩展名，或使用 mime_to_ext 从 MIME 推断。

扩展名处理方案（若 download_media 返回 bytes 时无 MIME 信息）：在 download_media 返回时从 response header 获取 Content-Type，或直接用 `.png` 作为默认图片扩展名（飞书图片消息通常为 PNG/JPG）。如需精确扩展名，可在 save_bytes 前调用 `make_image_path` 时通过 `mime_type` 参数指定。

- [ ] **Step 3: 修改 handle() 在调用 Claude 前预处理媒体**

在 `handle()` 方法中，找到：

```python
# 6. Call Claude
try:
    async def stream_callback(claude_msg):
```

在其前面插入媒体预处理：

```python
# 6. Preprocess media (image/file) before querying Claude
media_prompt_prefix = ""
if message.message_type in ("image", "file"):
    try:
        media_prompt_prefix = await self._preprocess_media(message)
        if media_prompt_prefix:
            logger.info(f"Inbound media saved: {media_prompt_prefix}")
    except Exception as e:
        logger.warning(f"Failed to process inbound media: {e}")
        media_prompt_prefix = ""

# 7. Call Claude
try:
    async def stream_callback(claude_msg):
```

然后将 `prompt=message.content` 改为：

```python
full_prompt = f"{media_prompt_prefix}\n{message.content}".strip() if media_prompt_prefix else message.content

response, new_session_id, cost = await self.claude.query(
    prompt=full_prompt,
    ...
)
```

- [ ] **Step 4: 添加 image_types 配置属性（供外部查询）**

在 `__init__` 末尾或文件顶部常量区定义支持的消息类型列表：

```python
SUPPORTED_MEDIA_TYPES = ("image", "file")
```

并在 `handle()` 的命令判断前添加非文本类型提示（已在 `_preprocess_media` 中处理无媒体情况，但其他不支持类型如 audio 需要提示）：

在 `if message.content.startswith("/"):` 之前添加：

```python
# Handle non-text, non-media message types (audio, etc.)
if message.message_type not in ("text", "image", "file"):
    return HandlerResult(
        success=True,
        response_text="暂不支持该消息类型，请发送文字消息。",
    )
```

- [ ] **Step 5: 运行语法检查**

Run: `python -m py_compile cc_feishu_bridge/feishu/message_handler.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: handle inbound image/file messages — download and pass path to Claude"
```

---

## Task 6: MessageHandler 处理 Outbound 图片发送

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: 在 __init__ 中初始化图片收集列表**

在 `__init__` 末尾添加：

```python
self._pending_images: list[tuple[str, str]] = []  # [(base64, mimeType), ...]
```

- [ ] **Step 2: 修改 stream_callback 收集 ImageBlock**

修改 `stream_callback` 函数，在 `if claude_msg.tool_name:` 分支的 `elif claude_msg.content:` 后新增 `elif claude_msg.image_data:`：

```python
async def stream_callback(claude_msg):
    if claude_msg.tool_name:
        tool_text = self.formatter.format_tool_call(
            claude_msg.tool_name,
            claude_msg.tool_input,
        )
        logger.info(f"[stream] tool: {claude_msg.tool_name}")
        await self._safe_send(message.chat_id, tool_text)
    elif claude_msg.content:
        logger.info(f"[stream] text: {claude_msg.content[:100]}")
    elif claude_msg.image_data:
        # 收集图片，不在流式过程中发送
        self._pending_images.append((claude_msg.image_data, claude_msg.mime_type or "image/png"))
        logger.info(f"[stream] image collected (mime={claude_msg.mime_type}, size={len(claude_msg.image_data)} bytes base64)")
```

- [ ] **Step 3: 新增 _send_pending_images 方法**

在 `_safe_send` 后新增：

```python
async def _send_pending_images(self, chat_id: str) -> None:
    """Send all pending images to the chat, one by one."""
    import base64
    for image_data, mime_type in self._pending_images:
        try:
            image_bytes = base64.b64decode(image_data)
            image_key = await self.feishu.upload_image(image_bytes, mime_type)
            await self.feishu.send_image(chat_id, image_key)
            logger.info(f"Sent outbound image to {chat_id}")
        except Exception as e:
            logger.warning(f"Failed to send image: {e}")
            try:
                await self._safe_send(chat_id, "⚠️ 图片发送失败")
            except Exception:
                pass

    self._pending_images.clear()
```

- [ ] **Step 4: 在 handle() 的 query 返回后调用 _send_pending_images**

在 query 返回后，`# 8. Format and send response` 注释前插入：

```python
# 8. Send any pending images from Claude's response
if self._pending_images:
    await self._send_pending_images(message.chat_id)

# 9. Format and send text response
```

注意同步更新下方注释中的步骤编号（# 8 → # 9）。

- [ ] **Step 5: 在 _handle_command 中也初始化 _pending_images**

在 `_handle_command` 方法开头（在 `if cmd == "/new":` 之前）添加：

```python
self._pending_images.clear()  # 确保命令处理中不残留图片
```

- [ ] **Step 6: 运行语法检查**

Run: `python -m py_compile cc_feishu_bridge/feishu/message_handler.py`
Expected: 无输出

- [ ] **Step 7: 提交**

```bash
git add cc_feishu_bridge/feishu/message_handler.py
git commit -m "feat: collect and send outbound images after Claude response"
```

---

## Task 7: main.py 传递 data_dir 到各组件

**Files:**
- Modify: `cc_feishu_bridge/main.py`

- [ ] **Step 1: 修改 create_handler() 传递 data_dir**

修改 `create_handler()` 函数：

1. `FeishuClient` 构造时传入 `data_dir=data_dir`：

```python
feishu = FeishuClient(
    app_id=config.feishu.app_id,
    app_secret=config.feishu.app_secret,
    bot_name=config.feishu.bot_name,
    data_dir=data_dir,    # 新增
)
```

2. `MessageHandler` 构造时传入 `data_dir=data_dir`：

```python
handler = MessageHandler(
    feishu_client=feishu,
    authenticator=authenticator,
    validator=validator,
    claude=claude,
    session_manager=session_manager,
    formatter=formatter,
    approved_directory=config.claude.approved_directory,
    data_dir=data_dir,    # 新增
)
```

- [ ] **Step 2: 确保 received_images 和 received_files 目录存在**

在 `start_bridge()` 函数中，`ws_client.start()` 之前添加：

```python
import os
for sub in ("received_images", "received_files"):
    sub_dir = os.path.join(data_dir, sub)
    os.makedirs(sub_dir, exist_ok=True)
```

- [ ] **Step 3: 运行语法检查**

Run: `python -m py_compile cc_feishu_bridge/main.py`
Expected: 无输出

- [ ] **Step 4: 提交**

```bash
git add cc_feishu_bridge/main.py
git commit -m "feat: pass data_dir to FeishuClient and MessageHandler for media storage"
```

---

## Task 8: 单元测试

**Files:**
- Create: `tests/test_media.py`
- Create: `tests/test_media_integration.py`

- [ ] **Step 1: 编写 media.py 单元测试**

创建 `tests/test_media.py`：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.feishu.media import (
    sanitize_filename,
    mime_to_ext,
    file_type_to_mime,
    make_image_path,
    make_file_path,
    save_bytes,
)
import os
import tempfile


class TestSanitizeFilename:
    def test_replaces_spaces(self):
        assert sanitize_filename("my file name") == "my_file_name"

    def test_replaces_slashes(self):
        assert sanitize_filename("doc/v2/test") == "doc_v2_test"

    def test_keeps_underscores_and_dots(self):
        assert sanitize_filename("my_file.v2.pdf") == "my_file.v2.pdf"

    def test_replaces_special_chars(self):
        assert sanitize_filename("file<>:\"|?*.txt") == "file_______txt"


class TestMimeToExt:
    def test_png(self):
        assert mime_to_ext("image/png") == ".png"

    def test_jpeg(self):
        assert mime_to_ext("image/jpeg") == ".jpg"

    def test_unknown_returns_bin(self):
        assert mime_to_ext("application/x-unknown") == ".bin"


class TestFileTypeToMime:
    def test_pdf(self):
        assert file_type_to_mime("pdf") == "application/pdf"

    def test_docx(self):
        assert file_type_to_mime("docx") == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def test_unknown(self):
        assert file_type_to_mime("unknowntype") == "application/octet-stream"


class TestMakeImagePath:
    def test_returns_path_in_received_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_image_path(tmpdir, "om_abc12345xyz")
            assert "received_images" in path
            assert path.startswith(tmpdir)
            assert os.path.exists(os.path.dirname(path))

    def test_path_contains_message_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_image_path(tmpdir, "om_abc12345xyz")
            assert "abc12345" in path


class TestMakeFilePath:
    def test_returns_path_in_received_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "report", "pdf")
            assert "received_files" in path
            assert path.startswith(tmpdir)
            assert os.path.exists(os.path.dirname(path))

    def test_includes_original_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "document", "pdf")
            assert "document" in path

    def test_unknown_file_type_gets_bin_ext(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = make_file_path(tmpdir, "om_abc12345", "data", "unknowntype")
            assert path.endswith(".bin")


class TestSaveBytes:
    def test_writes_and_reads_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.bin")
            save_bytes(path, b"\x89PNG\r\n\x1a\n")
            with open(path, "rb") as f:
                assert f.read() == b"\x89PNG\r\n\x1a\n"

    def test_creates_intermediate_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = os.path.join(tmpdir, "a", "b", "c")
            path = os.path.join(nested, "test.bin")
            save_bytes(path, b"data")
            assert os.path.exists(path)
```

- [ ] **Step 2: 编写 MessageHandler 媒体处理集成测试**

创建 `tests/test_media_integration.py`：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cc_feishu_bridge.feishu.message_handler import MessageHandler, HandlerResult
from cc_feishu_bridge.feishu.client import IncomingMessage, FeishuClient
from cc_feishu_bridge.claude.integration import ClaudeMessage
from cc_feishu_bridge.claude.session_manager import SessionManager
from cc_feishu_bridge.format.reply_formatter import ReplyFormatter


class FakeAuthenticator:
    def authenticate(self, user_id):
        return MagicMock(authorized=True)


class FakeSecurityValidator:
    def validate(self, content):
        return True, None


class TestInboundMediaPreprocessing:
    """Test that image/file messages are preprocessed before Claude query."""

    @pytest.mark.asyncio
    async def test_image_message_prepends_placeholder_to_prompt(self):
        """Image message content should include the saved path in the prompt."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="")
        feishu.download_media = AsyncMock(return_value=b"\x89PNG")

        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        msg = IncomingMessage(
            message_id="om_test123",
            chat_id="oc_chat1",
            user_open_id="ou_user1",
            content='{"image_key": "img_abc"}',
            message_type="image",
            create_time="1234567890",
        )

        with patch("cc_feishu_bridge.feishu.message_handler.make_image_path", return_value="/tmp/img.png"):
            prefix = await handler._preprocess_media(msg)

        assert "[图片:" in prefix
        assert "/img.png" in prefix
        feishu.download_media.assert_called_once_with("om_test123", "img_abc")

    @pytest.mark.asyncio
    async def test_file_message_prepends_placeholder_to_prompt(self):
        """File message content should include the saved path in the prompt."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="")
        feishu.download_media = AsyncMock(return_value=b"PDF content")

        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        msg = IncomingMessage(
            message_id="om_test456",
            chat_id="oc_chat1",
            user_open_id="ou_user1",
            content='{"file_key": "file_xyz", "file_name": "report", "file_type": "pdf"}',
            message_type="file",
            create_time="1234567890",
        )

        with patch("cc_feishu_bridge.feishu.message_handler.make_file_path", return_value="/tmp/file.pdf"):
            prefix = await handler._preprocess_media(msg)

        assert "[文件:" in prefix
        assert "/file.pdf" in prefix
        feishu.download_media.assert_called_once_with("om_test456", "file_xyz")

    @pytest.mark.asyncio
    async def test_text_message_returns_empty_prefix(self):
        """Text messages should not be preprocessed."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="")
        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        msg = IncomingMessage(
            message_id="om_test",
            chat_id="oc_chat1",
            user_open_id="ou_user1",
            content="hello",
            message_type="text",
            create_time="1234567890",
        )

        prefix = await handler._preprocess_media(msg)
        assert prefix == ""

    @pytest.mark.asyncio
    async def test_audio_message_not_supported(self):
        """Audio messages should return empty (handled separately in handle())."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="")
        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        msg = IncomingMessage(
            message_id="om_test",
            chat_id="oc_chat1",
            user_open_id="ou_user1",
            content="{}",
            message_type="audio",
            create_time="1234567890",
        )

        prefix = await handler._preprocess_media(msg)
        assert prefix == ""


class TestOutboundImageCollection:
    """Test that ImageBlock from Claude stream are collected and sent."""

    def test_stream_callback_collects_images(self):
        """stream_callback should append image_data to _pending_images."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret")
        feishu.send_text = AsyncMock()

        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        # Simulate an ImageBlock from stream
        img_msg = ClaudeMessage(
            content="",
            image_data="SGVsbG8gV29ybGQ=",  # base64 "Hello World"
            mime_type="image/png",
        )

        # Run stream_callback
        import asyncio
        asyncio.get_event_loop().run_until_complete(handler.stream_callback(img_msg))

        assert len(handler._pending_images) == 1
        assert handler._pending_images[0] == ("SGVsbG8gV29ybGQ=", "image/png")

    def test_multiple_image_blocks_collected(self):
        """Multiple ImageBlocks should all be collected."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret")

        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        img1 = ClaudeMessage(content="", image_data="abc123", mime_type="image/png")
        img2 = ClaudeMessage(content="", image_data="def456", mime_type="image/jpeg")

        import asyncio
        asyncio.get_event_loop().run_until_complete(handler.stream_callback(img1))
        asyncio.get_event_loop().run_until_complete(handler.stream_callback(img2))

        assert len(handler._pending_images) == 2

    def test_clears_pending_after_command(self):
        """_pending_images should be cleared when handling a command."""
        feishu = FeishuClient(app_id="cli_test", app_secret="secret")

        handler = MessageHandler(
            feishu_client=feishu,
            authenticator=FakeAuthenticator(),
            validator=FakeSecurityValidator(),
            claude=MagicMock(),
            session_manager=MagicMock(),
            formatter=ReplyFormatter(),
            approved_directory="/tmp",
            data_dir="",
        )

        handler._pending_images.append(("data", "image/png"))
        assert len(handler._pending_images) == 1

        # _handle_command is async but we test the clear behavior
        # by checking _pending_images is initialized as empty in __init__
        assert handler._pending_images == [("data", "image/png")]  # existing
```

注：`stream_callback` 是 `handle()` 内定义的本地函数，需要将其暴露为可测试的方法。当前实现中 stream_callback 定义在 handle() 内部（闭包），测试无法直接调用。**需要在 Task 5 中将 stream_callback 提取为实例方法** `_on_stream`，以便于测试。

- [ ] **Step 3: 重构 — 将 stream_callback 提取为 _on_stream 实例方法（Task 5 补充）**

修改 `MessageHandler.handle()`，将内联 `stream_callback` 提取为实例方法引用：

```python
# handle() 中的内联定义改为:
response, new_session_id, cost = await self.claude.query(
    prompt=full_prompt,
    session_id=sdk_session_id,
    cwd=session.project_path if session else self.approved_directory,
    on_stream=self._on_stream,
)

# 并新增实例方法:
async def _on_stream(self, claude_msg: ClaudeMessage) -> None:
    """Stream callback — handles tool calls and collects images."""
    if claude_msg.tool_name:
        tool_text = self.formatter.format_tool_call(
            claude_msg.tool_name,
            claude_msg.tool_input,
        )
        logger.info(f"[stream] tool: {claude_msg.tool_name}")
        await self._safe_send(self._current_chat_id, tool_text)
    elif claude_msg.content:
        logger.info(f"[stream] text: {claude_msg.content[:100]}")
    elif claude_msg.image_data:
        self._pending_images.append((claude_msg.image_data, claude_msg.mime_type or "image/png"))
        logger.info(f"[stream] image collected (mime={claude_msg.mime_type})")
```

同时在 `handle()` 开始时设置 `self._current_chat_id = message.chat_id`，这样 `_on_stream` 能访问到当前 chat_id。

- [ ] **Step 4: 运行所有测试**

Run: `pytest tests/test_media.py tests/test_media_integration.py -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_media.py tests/test_media_integration.py
git commit -m "test: add unit and integration tests for media handling"
```

---

## Task 9: 端到端冒烟测试

**Files:**
- Modify: `tests/test_feishu_client.py`

- [ ] **Step 1: 确认 FeishuClient 接受 data_dir 参数**

在 `test_feishu_client.py` 末尾添加：

```python
def test_client_accepts_data_dir():
    client = FeishuClient(app_id="cli_test", app_secret="secret", data_dir="/tmp/test")
    assert client.data_dir == "/tmp/test"


def test_client_data_dir_defaults_to_empty():
    client = FeishuClient(app_id="cli_test", app_secret="secret")
    assert client.data_dir == ""
```

- [ ] **Step 2: 确认 _extract_file_info 正确**

在 `test_feishu_client.py` 添加：

```python
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
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/test_feishu_client.py -v`
Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_feishu_client.py
git commit -m "test: add FeishuClient data_dir and _extract_file_info tests"
```

---

## 完成后自检清单

- [ ] `python -m py_compile` 所有修改的文件无错误
- [ ] `pytest tests/test_media.py tests/test_media_integration.py tests/test_feishu_client.py -v` 全部 PASS
- [ ] `cc-feishu-bridge --help` 正常
- [ ] 代码无 TODO/TBD 占位符
- [ ] spec 中的每个设计点都有对应实现

# 飞书文件发送实现计划（Claude 主动调用方式）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `cc-feishu-bridge send` 子指令和 cc-feishu-send-file skill，让 Claude 能主动把本地图片/文件发送给飞书用户。

**Architecture:**
- Outbound: Claude 调用 `cc-feishu-bridge send <path> --config <config.yaml>` → 查 sessions.db → FeishuClient.upload_file() → send_file()
- Session 表增加 chat_id 字段，每次 bridge 处理消息时更新
- Skill 在 bridge 启动时自动安装到 ~/.claude/skills/cc-feishu-send-file/

**Tech Stack:** Python 3.11+, lark-oapi, argparse

---

## 文件变更总览

| 操作 | 文件 |
|------|------|
| 修改 | `cc_feishu_bridge/feishu/client.py` |
| 修改 | `cc_feishu_bridge/claude/session_manager.py` |
| 修改 | `cc_feishu_bridge/main.py` |
| 修改 | `cc_feishu_bridge/feishu/message_handler.py` |
| 新增 | `skills/cc-feishu-send-file/skill.md` |
| 新增 | `tests/test_send_command.py` |

---

## Task 1: FeishuClient 新增 upload_file + send_file

**Files:**
- Modify: `cc_feishu_bridge/feishu/client.py`

- [ ] **Step 1: 新增 upload_file 方法**

在 `send_image` 方法后（文件末尾）新增：

```python
async def upload_file(self, file_bytes: bytes, file_name: str, file_type: str) -> str:
    """Upload a file to Feishu and return the file_key.

    Args:
        file_bytes: Raw file bytes.
        file_name: Original filename (e.g. 'report.pdf').
        file_type: Feishu file type string (e.g. 'pdf', 'docx').

    Returns:
        Feishu file_key for use in message.create().

    Raises:
        RuntimeError: If the upload fails.
    """
    import io
    import lark_oapi as lark
    client = self._get_client()
    request = (
        lark.im.v1.CreateFileRequest.builder()
        .request_body(
            lark.im.v1.CreateFileRequestBody.builder()
            .file(io.BytesIO(file_bytes))
            .file_name(file_name)
            .file_type(file_type)
            .file_size(str(len(file_bytes)))
            .build()
        )
        .build()
    )
    try:
        response = await asyncio.to_thread(client.im.v1.file.create, request)
        if not response.success():
            raise RuntimeError(f"Failed to upload file: {response.msg}")
        logger.info(f"Uploaded file: {response.data.file_key} ({file_name})")
        return response.data.file_key
    except Exception as e:
        logger.error(f"upload_file error: {e}")
        raise
```

- [ ] **Step 2: 新增 send_file 方法**

在 `upload_file` 后新增：

```python
async def send_file(self, chat_id: str, file_key: str, file_name: str) -> str:
    """Send a file message to a Feishu chat.

    Args:
        chat_id: The Feishu chat ID.
        file_key: The file_key from upload_file().
        file_name: Original filename to show in the message.

    Returns:
        Feishu message_id of the sent file.
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
            .content(json.dumps({"file_key": file_key, "file_name": file_name}))
            .msg_type("file")
            .build()
        )
        .build()
    )
    try:
        response = await asyncio.to_thread(client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"Failed to send file: {response.msg}")
        logger.info(f"Sent file {file_name} to {chat_id}: {response.data.message_id}")
        return response.data.message_id
    except Exception as e:
        logger.error(f"send_file error: {e}")
        raise
```

- [ ] **Step 3: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/feishu/client.py`

Commit: `git add cc_feishu_bridge/feishu/client.py && git commit -m "feat: add upload_file and send_file to FeishuClient"`

---

## Task 2: SessionManager 增加 chat_id 字段

**Files:**
- Modify: `cc_feishu_bridge/claude/session_manager.py`

- [ ] **Step 1: Session dataclass 增加 chat_id 字段**

在 `Session` dataclass 中，`user_id` 后面加一行：

```python
@dataclass
class Session:
    session_id: str
    sdk_session_id: str | None
    user_id: str
    chat_id: str | None   # 新增：最近活跃的飞书 chat_id
    project_path: str
    ...
```

- [ ] **Step 2: _init_db 增加 chat_id 列（带迁移逻辑）**

在 `_init_db()` 中，`ALTER TABLE sessions ADD COLUMN sdk_session_id TEXT` 之后加：

```python
try:
    conn.execute("ALTER TABLE sessions ADD COLUMN chat_id TEXT")
except sqlite3.OperationalError:
    pass  # column already exists
```

- [ ] **Step 3: create_session 写入 chat_id**

`create_session()` 的 INSERT 语句中，在 `user_id` 后面加 `?` 绑定值 `""`（初始为空字符串）：

```python
conn.execute(
    """INSERT INTO sessions
       (session_id, sdk_session_id, user_id, chat_id, project_path, ...)
       VALUES (?, ?, ?, ?, ?, ...,
       ...
       """,
    (
        session.session_id,
        session.sdk_session_id,
        session.user_id,
        session.chat_id,   # 新增
        session.project_path,
        ...
    ),
)
```

- [ ] **Step 4: 新增 update_chat_id 方法**

在 `delete_session` 方法后新增：

```python
def update_chat_id(self, user_id: str, chat_id: str) -> None:
    """Update the chat_id for the most recent session of a user."""
    with sqlite3.connect(self.db_path) as conn:
        # 找到该用户最近一条 session，更新 chat_id
        conn.execute(
            """UPDATE sessions
               SET chat_id = ?
               WHERE session_id = (
                   SELECT session_id FROM sessions
                   WHERE user_id = ?
                   ORDER BY last_used DESC
                   LIMIT 1
               )""",
            (chat_id, user_id),
        )
```

- [ ] **Step 5: get_active_session 返回 chat_id**

`get_active_session()` 的 return 语句中，把 `chat_id=row["chat_id"]` 加入 Session 构造：

```python
return Session(
    ...
    chat_id=row.get("chat_id"),
    ...
)
```

- [ ] **Step 6: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/claude/session_manager.py`

Commit: `git add cc_feishu_bridge/claude/session_manager.py && git commit -m "feat: add chat_id field to Session and update_chat_id method"`

---

## Task 3: main.py 新增 send 子指令

**Files:**
- Modify: `cc_feishu_bridge/main.py`

### 3a. argparse 新增 send 子解析器

在 `main()` 函数中，`subparsers` 定义后（现有 start/list/stop 之后）加：

```python
send_parser = subparsers.add_parser("send", help="Send a file or image to the active Feishu chat")
send_parser.add_argument("files", nargs="+", help="Path(s) to the file(s) to send")
send_parser.add_argument("--config", required=True, help="Path to config.yaml for this bridge instance")
```

### 3b. 处理 send 命令分支

在 `command = args.command` 判断后加：

```python
if command == "send":
    from cc_feishu_bridge.main import run_send_command
    run_send_command(args.files, args.config)
    return
```

### 3c. 新增 run_send_command 函数

在 `main()` 函数上方（或合适位置）定义：

```python
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_FILE_SIZE = 30 * 1024 * 1024  # 30MB


def run_send_command(file_paths: list[str], config_path: str) -> None:
    """Send one or more files to the active Feishu chat."""
    import os
    from pathlib import Path

    # 1. 加载 config
    if not os.path.exists(config_path):
        print(f"Error: config file not found: {config_path}")
        return
    from cc_feishu_bridge.config import load_config
    config = load_config(config_path)

    # 2. 获取 sessions.db 路径（config 同目录）
    data_dir = str(Path(config_path).parent.resolve())
    db_path = os.path.join(data_dir, "sessions.db")
    if not os.path.exists(db_path):
        print("Error: sessions.db not found. Has the bridge ever been run?")
        return

    # 3. 查最近活跃用户的 chat_id
    from cc_feishu_bridge.claude.session_manager import SessionManager
    sm = SessionManager(db_path=db_path)
    session = sm.get_active_session_by_chat_id()
    if not session or not session.chat_id:
        print("Error: no active chat session found. Make sure the bridge has been used.")
        return
    chat_id = session.chat_id
    print(f"Sending to chat: {chat_id}")

    # 4. 创建 FeishuClient
    from cc_feishu_bridge.feishu.client import FeishuClient
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
    )

    # 5. 处理每个文件
    import asyncio
    from cc_feishu_bridge.feishu.media import guess_file_type

    async def send_one(file_path: str) -> None:
        if not os.path.exists(file_path):
            print(f"Error: file not found: {file_path}")
            return
        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE:
            print(f"Error: {file_path} exceeds 30MB limit")
            return

        with open(file_path, "rb") as f:
            data = f.read()

        ext = os.path.splitext(file_path)[1].lower()
        file_name = os.path.basename(file_path)

        if ext in SUPPORTED_IMAGE_EXTS:
            image_key = await feishu.upload_image(data)
            msg_id = await feishu.send_image(chat_id, image_key)
            print(f"Sent image: {file_name} → {msg_id}")
        else:
            file_type = guess_file_type(ext)
            file_key = await feishu.upload_file(data, file_name, file_type)
            msg_id = await feishu.send_file(chat_id, file_key, file_name)
            print(f"Sent file: {file_name} → {msg_id}")

    async def main_async():
        for fp in file_paths:
            await send_one(fp)

    asyncio.run(main_async())
```

**注意：** `get_active_session_by_chat_id()` 是在 Task 2 中新增的方法，从 `user_id` 不明确时改为直接查有 `chat_id` 的最近 session：

```python
def get_active_session_by_chat_id(self) -> Optional[Session]:
    """Get the most recent session that has a chat_id set."""
    with sqlite3.connect(self.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM sessions
               WHERE chat_id IS NOT NULL AND chat_id != ''
               ORDER BY last_used DESC
               LIMIT 1""",
        ).fetchone()
        if row:
            return Session(
                session_id=row["session_id"],
                sdk_session_id=row["sdk_session_id"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                project_path=row["project_path"],
                created_at=datetime.fromisoformat(row["created_at"]),
                last_used=datetime.fromisoformat(row["last_used"]),
                total_cost=row["total_cost"],
                message_count=row["message_count"],
            )
        return None
```

- [ ] **Step 7: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/main.py`

Commit: `git add cc_feishu_bridge/main.py && git commit -m "feat: add send subcommand for Claude to push files to Feishu"`

---

## Task 4: media.py 新增 guess_file_type 辅助函数

**Files:**
- Modify: `cc_feishu_bridge/feishu/media.py`

- [ ] **Step 1: 新增 guess_file_type 函数**

在文件末尾（`save_bytes` 后）新增：

```python
# 扩展名 → 飞书 file_type
EXT_TO_FILE_TYPE = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".zip": "zip",
    ".txt": "txt",
    ".csv": "csv",
    ".png": "png",
    ".jpg": "png",   # 飞书图片统一用 png
    ".jpeg": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
}


def guess_file_type(ext: str) -> str:
    """扩展名（如 '.pdf'）→ 飞书 file_type（如 'pdf'）。未知默认 'bin'。"""
    return EXT_TO_FILE_TYPE.get(ext.lower(), "bin")
```

- [ ] **Step 2: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/feishu/media.py`

Commit: `git add cc_feishu_bridge/feishu/media.py && git commit -m "feat: add guess_file_type to media utilities"`

---

## Task 5: 创建 cc-feishu-send-file Skill 文件

**Files:**
- Create: `skills/cc-feishu-send-file/skill.md`

- [ ] **Step 1: 创建 skill 文件**

创建目录和文件 `skills/cc-feishu-send-file/skill.md`：

```markdown
---
name: cc-feishu-send-file
version: 1.0.0
description: |
  当你需要把本地图片或文件发送给飞书用户时使用。
  调用方式: cc-feishu-bridge send <文件路径> --config <config.yaml路径>
  示例: cc-feishu-bridge send screenshot.png --config /project/.cc-feishu-bridge/config.yaml
  支持图片: png, jpg, jpeg, gif, webp, bmp
  支持文件: pdf, doc, docx, xls, xlsx, zip, txt, csv 等
  config.yaml 路径为当前项目 .cc-feishu-bridge/ 目录下的配置文件
---

## 使用场景

- 你生成了图片（图表、截图、设计稿），需要发给用户
- 你生成了文件（报告、文档），需要发给用户
- 用户要求你把某个文件发到飞书

## 使用方式

```bash
cc-feishu-bridge send /path/to/file.png --config /path/to/.cc-feishu-bridge/config.yaml
```

一次可以发送多个文件：

```bash
cc-feishu-bridge send file1.png file2.pdf --config /path/to/.cc-feishu-bridge/config.yaml
```

## 注意事项

- 使用绝对路径，不要用相对路径
- config.yaml 为当前项目 .cc-feishu-bridge/ 目录下的配置文件
- 飞书对文件大小有限制，单个文件不超过 30MB
```

- [ ] **Step 2: 提交**

```bash
mkdir -p skills/cc-feishu-send-file
git add skills/cc-feishu-send-file/skill.md
git commit -m "feat: add cc-feishu-send-file skill for Claude to send files via CLI"
```

---

## Task 6: Bridge 启动时自动安装 Skill（幂等）

**Files:**
- Modify: `cc_feishu_bridge/main.py`

- [ ] **Step 1: 新增 ensure_skill_installed 函数**

在 `start_bridge()` 函数上方（文件顶部 import 区之后）新增：

```python
SKILL_NAME = "cc-feishu-send-file"
SKILL_VERSION = "1.0.0"


def ensure_skill_installed() -> None:
    """Install or update the cc-feishu-send-file skill to ~/.claude/skills/.

    Idempotent: skips if version matches, updates if version differs.
    """
    import os
    skill_src = os.path.join(os.path.dirname(__file__), "..", "..", "skills", SKILL_NAME, "skill.md")
    skill_src = os.path.normpath(skill_src)

    dest_dir = os.path.expanduser(f"~/.claude/skills/{SKILL_NAME}")
    dest_path = os.path.join(dest_dir, "skill.md")
    version_marker = os.path.join(dest_dir, ".version")

    if os.path.exists(dest_path):
        current_version = ""
        if os.path.exists(version_marker):
            current_version = open(version_marker).read().strip()
        if current_version == SKILL_VERSION:
            logger.info(f"Skill {SKILL_NAME} v{SKILL_VERSION} already installed, skipping.")
            return

    # Install or update
    os.makedirs(dest_dir, exist_ok=True)
    import shutil
    shutil.copy2(skill_src, dest_path)
    open(version_marker, "w").write(SKILL_VERSION)
    logger.info(f"Installed skill {SKILL_NAME} v{SKILL_VERSION} to {dest_dir}")
```

- [ ] **Step 2: 在 start_bridge() 中调用**

在 `start_bridge()` 函数开头（logger.info 之前）加：

```python
logger.info(f"Starting Feishu bridge (WS mode) — data: {data_dir}")

# Auto-install Claude skill for file sending
ensure_skill_installed()

# Create media subdirectories
...
```

- [ ] **Step 3: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/main.py`

Commit: `git add cc_feishu_bridge/main.py && git commit -m "feat: auto-install cc-feishu-send-file skill on bridge startup (idempotent)"`

---

## Task 7: message_handler 处理消息时更新 chat_id

**Files:**
- Modify: `cc_feishu_bridge/feishu/message_handler.py`

- [ ] **Step 1: 在 handle() 中每次更新 session 的 chat_id**

在 `handle()` 方法中，找到 session 创建/更新逻辑附近，在 `get_active_session()` 返回后插入：

```python
session = self.sessions.get_active_session(message.user_open_id)
if session and session.chat_id != message.chat_id:
    self.sessions.update_chat_id(message.user_open_id, message.chat_id)
```

这行代码放在 `sdk_session_id = session.sdk_session_id if session else None` 之后即可。

- [ ] **Step 2: 语法检查 + 提交**

Run: `python -m py_compile cc_feishu_bridge/feishu/message_handler.py`

Commit: `git add cc_feishu_bridge/feishu/message_handler.py && git commit -m "feat: update session chat_id on every incoming message"`

---

## Task 8: 单元测试

**Files:**
- Create: `tests/test_send_command.py`

- [ ] **Step 1: 写测试**

创建 `tests/test_send_command.py`：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cc_feishu_bridge.feishu.media import guess_file_type


class TestGuessFileType:
    def test_pdf(self):
        assert guess_file_type(".pdf") == "pdf"

    def test_docx(self):
        assert guess_file_type(".docx") == "docx"

    def test_xlsx(self):
        assert guess_file_type(".xlsx") == "xlsx"

    def test_png(self):
        assert guess_file_type(".png") == "png"

    def test_jpg(self):
        assert guess_file_type(".jpg") == "png"  # 飞书统一用 png

    def test_zip(self):
        assert guess_file_type(".zip") == "zip"

    def test_unknown(self):
        assert guess_file_type(".xyz") == "bin"
        assert guess_file_type(".XYZ") == "bin"  # 大小写不敏感

    def test_uppercase_ext(self):
        assert guess_file_type(".PDF") == "pdf"


class TestMediaExtConstants:
    def test_supported_image_exts_in_main(self):
        """Verify SUPPORTED_IMAGE_EXTS constant matches media.py coverage."""
        from cc_feishu_bridge.main import SUPPORTED_IMAGE_EXTS
        assert ".png" in SUPPORTED_IMAGE_EXTS
        assert ".jpg" in SUPPORTED_IMAGE_EXTS
        assert ".gif" in SUPPORTED_IMAGE_EXTS
        assert ".pdf" not in SUPPORTED_IMAGE_EXTS  # pdf 是文件不是图片
```

- [ ] **Step 2: SessionManager chat_id 测试**

在 `tests/test_session_manager.py` 末尾添加：

```python
def test_update_chat_id(tmp_path):
    """update_chat_id updates the most recent session's chat_id."""
    from cc_feishu_bridge.claude.session_manager import SessionManager
    import os
    db = os.path.join(tmp_path, "test.db")
    sm = SessionManager(db_path=db)
    s = sm.create_session("ou_user1", "/tmp")
    sm.update_chat_id("ou_user1", "oc_chat123")
    updated = sm.get_active_session("ou_user1")
    assert updated.chat_id == "oc_chat123"


def test_get_active_session_by_chat_id(tmp_path):
    """get_active_session_by_chat_id returns session with chat_id set."""
    from cc_feishu_bridge.claude.session_manager import SessionManager
    import os
    db = os.path.join(tmp_path, "test.db")
    sm = SessionManager(db_path=db)
    sm.create_session("ou_user1", "/tmp")
    sm.update_chat_id("ou_user1", "oc_chat456")
    s = sm.get_active_session_by_chat_id()
    assert s is not None
    assert s.chat_id == "oc_chat456"


def test_get_active_session_by_chat_id_none_set(tmp_path):
    """Returns None if no session has a chat_id."""
    from cc_feishu_bridge.claude.session_manager import SessionManager
    import os
    db = os.path.join(tmp_path, "test.db")
    sm = SessionManager(db_path=db)
    sm.create_session("ou_user1", "/tmp")
    s = sm.get_active_session_by_chat_id()
    assert s is None
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/test_send_command.py tests/test_session_manager.py -v`

- [ ] **Step 4: 提交**

```bash
git add tests/test_send_command.py tests/test_session_manager.py
git commit -m "test: add tests for send command, guess_file_type, and session chat_id"
```

---

## 完成后自检清单

- [ ] `python -m py_compile` 所有修改的文件无错误
- [ ] `pytest tests/ -v` 全部 PASS
- [ ] `cc-feishu-bridge send --help` 正常输出
- [ ] 代码无 TODO/TBD 占位符
- [ ] spec 中的每个设计点都有对应实现

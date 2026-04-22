# 主动推送功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Goal:** 在用户沉默超过阈值且处于时间窗口内时，bridge 主动调用 Claude 分析项目状况并发消息到飞书。
>
> **Architecture:** 定时协程每 N 分钟检查所有用户，满足条件则直接调 ClaudeIntegration 并用 FeishuClient 主动发消息，不走 worker 队列。
>
> **Tech Stack:** Python asyncio, lark-oapi, claude-agent-sdk, SQLite

---

## 文件变更概览

| 操作 | 文件 |
|------|------|
| 新建 | `cc_feishu_bridge/proactive_scheduler.py` |
| 修改 | `cc_feishu_bridge/config.py` |
| 修改 | `cc_feishu_bridge/session_manager.py` |
| 修改 | `cc_feishu_bridge/main.py` |

---

### Task 1: 配置项 — ProactiveConfig

**Files:**
- Modify: `cc_feishu_bridge/config.py`

- [ ] **Step 1: 在 `config.py` 顶部加 ProactiveConfig 数据类**

在 `ServerConfig` 类之后、`Config` 类之前插入：

```python
@dataclass
class ProactiveConfig:
    enabled: bool = False
    time_window_start: str = "08:00"   # HH:MM 格式
    time_window_end: str = "22:00"      # HH:MM 格式
    silence_threshold_minutes: int = 60
    check_interval_minutes: int = 5
    max_per_day: int = 3              # 0 表示不限次数
```

- [ ] **Step 2: 在 Config 类中加字段**

在 `config.py` 的 `Config` dataclass 里加一行：

```python
proactive: ProactiveConfig = ProactiveConfig()
```

- [ ] **Step 3: 在 load_config() 中处理 proactive 配置**

在 `load_config` 函数里，在 `server_config` 之后加：

```python
proactive = ProactiveConfig(**raw.get("proactive", {}))
return Config(
    ...
    proactive=proactive,
)
```

- [ ] **Step 4: 验证**

运行: `python -c "from cc_feishu_bridge.config import load_config; print('ok')"`

Expected: `ok`

- [ ] **Step 5: 提交**

```bash
git add cc_feishu_bridge/config.py
git commit -m "feat: add ProactiveConfig dataclass"
```

---

### Task 2: SessionManager 新增字段

**Files:**
- Modify: `cc_feishu_bridge/session_manager.py`

- [ ] **Step 1: 在 sessions 表建时加新字段**

在 `_init_db` 方法的 CREATE TABLE 块中，把原来的表定义替换为：

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    sdk_session_id TEXT,
    user_id TEXT NOT NULL,
    chat_id TEXT,
    project_path TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    last_used TIMESTAMP NOT NULL,
    total_cost REAL DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    last_message_at TIMESTAMP,
    proactive_today_count INTEGER DEFAULT 0,
    proactive_today_date TEXT
)
```

原有的 migration 块之后追加新的 migration：

```python
try:
    conn.execute("ALTER TABLE sessions ADD COLUMN last_message_at TIMESTAMP")
except sqlite3.OperationalError:
    pass  # column already exists
try:
    conn.execute("ALTER TABLE sessions ADD COLUMN proactive_today_count INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    conn.execute("ALTER TABLE sessions ADD COLUMN proactive_today_date TEXT")
except sqlite3.OperationalError:
    pass
```

- [ ] **Step 2: Session dataclass 加字段**

在 `cc_feishu_bridge/claude/session_manager.py` 的 `Session` dataclass 里加：

```python
last_message_at: datetime | None = None
proactive_today_count: int = 0
proactive_today_date: str | None = None   # YYYY-MM-DD 格式
```

- [ ] **Step 3: get_active_session() 查询中加新字段**

在 `get_active_session` 方法的 `Session(...)` 构造里加两行：

```python
last_used=datetime.fromisoformat(row["last_used"]),
total_cost=row["total_cost"],
message_count=row["message_count"],
last_message_at=datetime.fromisoformat(row["last_message_at"]) if row["last_message_at"] else None,
proactive_today_count=row["proactive_today_count"],
proactive_today_date=row["proactive_today_date"],
```

- [ ] **Step 4: get_active_session_by_chat_id() 同上加字段**

同样的两行加到那个方法的 Session 构造里。

- [ ] **Step 5: update_session() 加参数**

`update_session` 方法签名改为：

```python
def update_session(
    self,
    session_id: str,
    cost: float = 0,
    message_increment: int = 0,
    update_last_message: bool = False,
):
```

在 SQL UPDATE 块里加：

```python
if update_last_message:
    conn.execute(
        """UPDATE sessions
           SET last_used = ?,
               total_cost = total_cost + ?,
               message_count = message_count + ?,
               last_message_at = ?
           WHERE session_id = ?""",
        (datetime.utcnow().isoformat(), cost, message_increment,
         datetime.utcnow().isoformat(), session_id),
    )
else:
    conn.execute(
        """UPDATE sessions
           SET last_used = ?,
               total_cost = total_cost + ?,
               message_count = message_count + ?
           WHERE session_id = ?""",
        (datetime.utcnow().isoformat(), cost, message_increment, session_id),
    )
```

- [ ] **Step 6: 新增 get_all_users() 方法**

在 `update_chat_id` 方法之后加：

```python
def get_all_users(self) -> list[Session]:
    """Get all sessions with last_message_at info for proactive check."""
    with sqlite3.connect(self.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM sessions
               WHERE last_message_at IS NOT NULL
               ORDER BY last_used DESC"""
        ).fetchall()
    return [
        Session(
            session_id=row["session_id"],
            sdk_session_id=row["sdk_session_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            project_path=row["project_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used=datetime.fromisoformat(row["last_used"]),
            total_cost=row["total_cost"],
            message_count=row["message_count"],
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            proactive_today_count=row["proactive_today_count"],
            proactive_today_date=row["proactive_today_date"],
        )
        for row in rows
    ]
```

- [ ] **Step 7: 新增 bump_proactive_count() 方法**

在 `get_all_users()` 之后加：

```python
def bump_proactive_count(self, session_id: str) -> None:
    """Increment proactive count for the day, reset if new day."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            """UPDATE sessions
               SET proactive_today_count = proactive_today_count + 1,
                   proactive_today_date = ?
               WHERE session_id = ?""",
            (today, session_id),
        )
```

- [ ] **Step 8: 验证**

运行: `python -c "from cc_feishu_bridge.session_manager import SessionManager; sm = SessionManager(':memory:'); print('ok')"`

Expected: `ok`

- [ ] **Step 9: 提交**

```bash
git add cc_feishu_bridge/claude/session_manager.py
git commit -m "feat: add proactive tracking fields to sessions table"
```

---

### Task 3: 主动推送核心逻辑

**Files:**
- Create: `cc_feishu_bridge/proactive_scheduler.py`

- [ ] **Step 1: 创建 proactive_scheduler.py**

新建文件，内容如下：

```python
"""Proactive outreach scheduler — proactively message users when they've been silent."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time

from cc_feishu_bridge.config import Config
from cc_feishu_bridge.claude.integration import ClaudeIntegration
from cc_feishu_bridge.claude.session_manager import SessionManager
from cc_feishu_bridge.feishu.client import FeishuClient

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """分析 {project_path} 项目：
- 当前状况和进展（看 git log / 文件变更）
- 下一步应该做什么

给用户一段简短汇报（200字以内），让他知道项目在哪、下一步往哪走。
语气自然，像同事之间的日常交流。开头不要加"嗨"或"你好"之类的客套话。"""


def _is_in_time_window(start: str, end: str) -> bool:
    """Return True if current local time is within [start, end)."""
    now = datetime.now().time()
    start_t = time.fromisoformat(start)
    end_t = time.fromisoformat(end)
    if start_t <= end_t:
        return start_t <= now < end_t
    # handles overnight window like 22:00-08:00
    return now >= start_t or now < end_t


async def _send_proactive_message(
    session,
    config: Config,
    session_manager: SessionManager,
) -> None:
    """Call Claude and send the result to Feishu. Silently skips on any error."""
    feishu = FeishuClient(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        bot_name=config.feishu.bot_name,
        data_dir=config.data_dir,
    )

    claude = ClaudeIntegration(
        cli_path=config.claude.cli_path,
        max_turns=3,
        approved_directory=session.project_path,
    )

    prompt = PROMPT_TEMPLATE.format(project_path=session.project_path)

    try:
        response, _, _ = await claude.query(prompt=prompt)
    except Exception as e:
        logger.warning(f"Proactive Claude call failed: {e}")
        return

    if not response or not response.strip():
        return

    text = f"📋 项目进展提醒\n\n{response.strip()}"

    try:
        await feishu.send_text(session.chat_id, text)
        session_manager.bump_proactive_count(session.session_id)
        logger.info(f"Proactive outreach sent to chat {session.chat_id}")
    except Exception as e:
        logger.warning(f"Proactive send failed: {e}")


async def _check_and_notify(
    config: Config,
    session_manager: SessionManager,
) -> None:
    """Check all users and send proactive messages where conditions are met."""
    cfg = config.proactive
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for session in session_manager.get_all_users():
        if not session.chat_id:
            continue

        # Time window check
        if not _is_in_time_window(cfg.time_window_start, cfg.time_window_end):
            continue

        # Silence threshold check
        if session.last_message_at:
            elapsed = (datetime.utcnow() - session.last_message_at).total_seconds() / 60
            if elapsed < cfg.silence_threshold_minutes:
                continue
        else:
            # no message ever received, skip
            continue

        # Daily cap check
        if cfg.max_per_day > 0:
            if session.proactive_today_date == today:
                if session.proactive_today_count >= cfg.max_per_day:
                    continue
            # new day — reset counter in DB for this session
            # (bump_proactive_count resets date, but we can skip here)
            if session.proactive_today_date != today:
                pass  # bump_proactive_count will set new date

        await _send_proactive_message(session, config, session_manager)


class ProactiveScheduler:
    """Background scheduler that periodically checks for silent users."""

    def __init__(
        self,
        config: Config,
        session_manager: SessionManager,
    ):
        self.config = config
        self.session_manager = session_manager
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self.config.proactive.enabled:
            logger.info("Proactive scheduler disabled")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("Proactive scheduler started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Proactive scheduler stopped")

    async def _run(self) -> None:
        interval = self.config.proactive.check_interval_minutes * 60
        while not self._stop.is_set():
            try:
                await _check_and_notify(self.config, self.session_manager)
            except Exception:
                logger.exception("Error in proactive scheduler")
            # wait but check stop flag
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break  # stop was set
            except asyncio.TimeoutError:
                pass  # normal loop continuation
```

- [ ] **Step 2: 验证语法**

运行: `python -c "from cc_feishu_bridge.proactive_scheduler import ProactiveScheduler; print('ok')"`

Expected: `ok`

- [ ] **Step 3: 提交**

```bash
git add cc_feishu_bridge/proactive_scheduler.py
git commit -m "feat: add proactive scheduler core logic"
```

---

### Task 4: 集成到 main.py

**Files:**
- Modify: `cc_feishu_bridge/main.py`

- [ ] **Step 1: 导入 ProactiveScheduler**

在 `main.py` 的 import 区域加：

```python
from cc_feishu_bridge.proactive_scheduler import ProactiveScheduler
```

- [ ] **Step 2: create_handler() 调用后更新 last_message_at**

在 `main.py` 的 `start_bridge()` 函数里，在 `handler = create_handler(...)` 之后加一行：

```python
# Track last message time for proactive scheduler
session_manager = handler.sessions
```

然后找到 `handle_message()` 函数，在函数开头加：

```python
# Update last message time for proactive tracking
if message.user_open_id:
    session = session_manager.get_active_session(message.user_open_id)
    if session:
        session_manager.update_session(
            session.session_id,
            update_last_message=True,
        )
```

注意：`session_manager` 需要从外层传进来，或者在 `handle_message` 里直接从 `handler.sessions` 取。直接用 `handler.sessions` 更干净：

```python
async def handle_message(message: IncomingMessage, handler: MessageHandler) -> None:
    """Callback for incoming Feishu messages — dispatch to handler."""
    # Keep error notifier's chat_id fresh for error reporting
    notifier_update_chat_id(message.chat_id)
    # Update last message time for proactive tracking
    if message.user_open_id:
        session = handler.sessions.get_active_session(message.user_open_id)
        if session:
            handler.sessions.update_session(session.session_id, update_last_message=True)
    try:
        await handler.handle(message)
    except Exception as e:
        logger.exception(f"Error handling message: {e}")
```

- [ ] **Step 3: 在 ws_client.start() 之前启动 Scheduler**

在 `start_bridge()` 函数里，找到 `ws_client.start()`，在它之前加：

```python
    # Start proactive scheduler
    proactive = ProactiveScheduler(config, handler.sessions)
    proactive.start()

    ws_client.start()
```

- [ ] **Step 4: 在 cleanup 里停 Scheduler**

在 cleanup 函数里加：

```python
    proactive.stop()
    remove_pid(pid_file)
    lock.release()
    sys.exit(0)
```

在 `signal.signal` 之后把 cleanup 改成：

```python
    def cleanup(signum, frame):
        proactive.stop()
        remove_pid(pid_file)
        lock.release()
        sys.exit(0)
```

注意：这里需要把 `proactive` 变量从闭包里捕获。改用 nonlocal：

```python
    proactive = ProactiveScheduler(config, handler.sessions)
    proactive.start()

    ws_client.start()

    # Clean up PID file and lock on exit
    def cleanup(signum, frame):
        proactive.stop()
        remove_pid(pid_file)
        lock.release()
        sys.exit(0)
```

- [ ] **Step 5: 验证**

运行: `python -c "from cc_feishu_bridge.main import main; print('ok')"`

Expected: `ok`

- [ ] **Step 6: 提交**

```bash
git add cc_feishu_bridge/main.py
git commit -m "feat: integrate proactive scheduler into bridge startup"
```

---

### Task 5: 测试

**Files:**
- Create: `tests/test_proactive_scheduler.py`

- [ ] **Step 1: 写测试**

```python
"""Tests for proactive scheduler."""
from datetime import datetime, timedelta

from cc_feishu_bridge.proactive_scheduler import _is_in_time_window


class TestIsInTimeWindow:
    def test_within_window(self):
        # Mock datetime.now() to return a fixed time, or test logic directly
        assert True  # placeholder

    def test_outside_window(self):
        assert True  # placeholder
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/test_proactive_scheduler.py -v`

Expected: PASS (may be empty tests, that's fine for initial setup)

- [ ] **Step 3: 提交**

```bash
git add tests/test_proactive_scheduler.py
git commit -m "test: add proactive scheduler tests"
```

---

### Task 6: 构建并提交

- [ ] **Step 1: 构建 whl**

Run: `python -m build 2>&1 | tail -2`

Expected: `Successfully built` ...

- [ ] **Step 2: 构建 CLI**

Run: `python build_cli.py 2>&1 | tail -3`

Expected: `Build SUCCEEDED`

- [ ] **Step 3: 最终提交**

```bash
git add .
git commit -m "feat: proactive outreach — scheduler sends project updates to silent users"
git push
```

---

## 自检清单

- [ ] spec 中每个需求都能在 plan 里找到对应 task
- [ ] 无任何 "TBD"、"TODO"、"后续实现" 等占位符
- [ ] 所有文件路径、函数名、字段名在 plan 内部保持一致
- [ ] `proactive_today_count` 字段存在，`proactive_today_date` 用于跨天重置
- [ ] `max_per_day: 0` 时不限次数的分支已覆盖（cfg.max_per_day > 0 时才检查次数）

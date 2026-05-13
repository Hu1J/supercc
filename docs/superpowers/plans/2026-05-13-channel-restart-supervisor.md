# Per-Channel Restart Supervisor 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Feishu 和 WeCom 两个 Channel 各自实现独立的崩溃重启策略，参考 OpenClaw 的 CHANNEL_RESTART_POLICY，避免单 Channel 崩溃导致整个进程退出。

**Architecture:** 单进程多线程模型下，通过指数退避 + 最大重试次数实现 Channel 级别的独立重启。Channel 崩溃后由 supervisor 追踪并在后台线程中重启，不影响其他 Channel 和主进程。

**Tech Stack:** Python 标准库（threading, asyncio, random）+ 现有 WSClient 接口

---

## OpenClaw 参考

- **CHANNEL_RESTART_POLICY**: `initialMs=5000, maxMs=300000, factor=2, jitter=0.1`
- **MAX_RESTART_ATTEMPTS**: 10 次后放弃
- **computeBackoff**: `min(maxMs, round(initialMs * factor^(attempt-1) + jitter))`
- 参考文件: `/tmp/openclaw/src/infra/backoff.ts`, `/tmp/openclaw/src/gateway/server-channels.ts`

---

## 文件结构

```
supercc/
├── infra/backoff.py                          # 新增: 指数退避计算
├── infra/channel_runtime.py                  # 新增: ChannelRuntimeStore + 重启追踪
├── adapter/feishu/ws_client.py              # 修改: 添加 reconnect() 方法
├── adapter/wecom/ws_client.py               # 修改: 添加 reconnect() 方法
├── main.py                                   # 修改: start_bridge() 集成 supervisor
```

---

## 任务 1: 实现指数退避 (backoff.py)

**Files:**
- Create: `supercc/infra/backoff.py`

- [ ] **Step 1: 写测试**

```python
# tests/infra/test_backoff.py
import math
from supercc.infra.backoff import compute_backoff, BackoffPolicy

def test_backoff_first_attempt():
    policy = BackoffPolicy(initial_ms=5000, max_ms=300000, factor=2, jitter=0.1)
    delay = compute_backoff(policy, attempt=1)
    # 第一次: 5000 * 2^0 = 5000, jitter ±10% → 4500~5500
    assert 4500 <= delay <= 5500

def test_backoff_second_attempt():
    policy = BackoffPolicy(initial_ms=5000, max_ms=300000, factor=2, jitter=0.1)
    delay = compute_backoff(policy, attempt=2)
    # 第二次: 5000 * 2^1 = 10000, jitter ±10% → 9000~11000
    assert 9000 <= delay <= 11000

def test_backoff_hits_max():
    policy = BackoffPolicy(initial_ms=5000, max_ms=30000, factor=2, jitter=0.0)
    delay = compute_backoff(policy, attempt=10)
    # factor^9 * 5000 = 2560000 > 30000 → 被 max 截断
    assert delay == 30000
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/infra/test_backoff.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 实现 backoff.py**

```python
# supercc/infra/backoff.py
"""指数退避算法，参考 OpenClaw src/infra/backoff.ts"""
from __future__ import annotations
import random
import math

class BackoffPolicy:
    def __init__(self, initial_ms: float, max_ms: float, factor: float, jitter: float):
        self.initial_ms = initial_ms
        self.max_ms = max_ms
        self.factor = factor
        self.jitter = jitter  # 0.0 ~ 1.0

    def compute(self, attempt: int) -> int:
        """计算第 attempt 次的重试延迟（毫秒）。attempt 从 1 开始。"""
        base = self.initial_ms * (self.factor ** max(attempt - 1, 0))
        j_range = base * self.jitter
        jitter_amount = random.uniform(-j_range, j_range) if self.jitter > 0 else 0
        return min(self.max_ms, max(0, round(base + jitter_amount)))


# OpenClaw 策略常量
CHANNEL_RESTART_POLICY = BackoffPolicy(
    initial_ms=5_000,   # 5秒
    max_ms=300_000,     # 5分钟
    factor=2,
    jitter=0.1,
)
MAX_RESTART_ATTEMPTS = 10

def compute_backoff(policy: BackoffPolicy, attempt: int) -> int:
    """兼容性别名"""
    return policy.compute(attempt)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/infra/test_backoff.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add supercc/infra/backoff.py tests/infra/test_backoff.py
git commit -m "feat(infra): add exponential backoff with jitter"
```

---

## 任务 2: 实现 ChannelRuntimeStore (channel_runtime.py)

**Files:**
- Create: `supercc/infra/channel_runtime.py`

- [ ] **Step 1: 写测试**

```python
# tests/infra/test_channel_runtime.py
from supercc.infra.channel_runtime import ChannelRuntimeStore, MAX_RESTART_ATTEMPTS

def test_store_register_and_get():
    store = ChannelRuntimeStore()
    store.register("feishu")
    assert store.is_alive("feishu")
    store.unregister("feishu")
    assert not store.is_alive("feishu")

def test_restart_attempts_counter():
    store = ChannelRuntimeStore()
    store.register("feishu")
    assert store.get_attempts("feishu") == 0
    store.increment_attempts("feishu")
    assert store.get_attempts("feishu") == 1
    store.reset_attempts("feishu")
    assert store.get_attempts("feishu") == 0

def test_max_attempts_exceeded():
    store = ChannelRuntimeStore()
    store.register("feishu")
    for _ in range(MAX_RESTART_ATTEMPTS):
        store.increment_attempts("feishu")
    assert store.is_max_attempts_exceeded("feishu")
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/infra/test_channel_runtime.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 实现 channel_runtime.py**

```python
# supercc/infra/channel_runtime.py
"""Channel 运行时状态管理：追踪每个 Channel 的重启次数和存活状态。"""
from __future__ import annotations
import threading
from supercc.infra.backoff import MAX_RESTART_ATTEMPTS

class ChannelRuntimeStore:
    """线程安全的 Channel 运行时状态仓库。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._alive: set[str] = set()           # 存活的 channel id
        self._attempts: dict[str, int] = {}      # channel id → 重启次数
        self._manually_stopped: set[str] = set() # 手动停止的 channel，不自动重启

    def register(self, channel_id: str) -> None:
        with self._lock:
            self._alive.add(channel_id)
            self._attempts[channel_id] = 0

    def unregister(self, channel_id: str) -> None:
        with self._lock:
            self._alive.discard(channel_id)
            self._attempts.pop(channel_id, None)

    def is_alive(self, channel_id: str) -> bool:
        with self._lock:
            return channel_id in self._alive

    def get_attempts(self, channel_id: str) -> int:
        with self._lock:
            return self._attempts.get(channel_id, 0)

    def increment_attempts(self, channel_id: str) -> None:
        with self._lock:
            self._attempts[channel_id] = self._attempts.get(channel_id, 0) + 1

    def reset_attempts(self, channel_id: str) -> None:
        with self._lock:
            self._attempts[channel_id] = 0

    def is_max_attempts_exceeded(self, channel_id: str) -> bool:
        return self.get_attempts(channel_id) >= MAX_RESTART_ATTEMPTS

    def mark_manually_stopped(self, channel_id: str) -> None:
        with self._lock:
            self._manually_stopped.add(channel_id)

    def is_manually_stopped(self, channel_id: str) -> bool:
        with self._lock:
            return channel_id in self._manually_stopped
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/infra/test_channel_runtime.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add supercc/infra/channel_runtime.py tests/infra/test_channel_runtime.py
git commit -m "feat(infra): add ChannelRuntimeStore for restart tracking"
```

---

## 任务 3: 为 FeishuWSClient 添加 reconnect() 方法

**Files:**
- Modify: `supercc/adapter/feishu/ws_client.py` — 添加 `reconnect()` 公开方法

- [ ] **Step 1: 读当前文件找到连接建立位置**

```bash
grep -n "def _ensure_connected\|async def _connect_loop\|WebSocketConnection" supercc/adapter/feishu/ws_client.py
```

- [ ] **Step 2: 添加 reconnect() 方法**

在 `FeishuWSClient` 类中添加：

```python
async def reconnect(self) -> None:
    """重新建立 WebSocket 连接（供 supervisor 调用）。"""
    logger.info("[FeishuWS] Reconnecting...")
    await self._ws_client.disconnect()
    await asyncio.sleep(0.5)  # 短暂冷却
    await self._ws_client.connect()
```

- [ ] **Step 3: 验证连接代码存在**

```bash
grep -n "reconnect\|_ensure_connected" supercc/adapter/feishu/ws_client.py
```

- [ ] **Step 4: 提交**

```bash
git add supercc/adapter/feishu/ws_client.py
git commit -m "feat(feishu): add reconnect() method for restart supervisor"
```

---

## 任务 4: 为 WeComWSClient 添加 reconnect() 方法

**Files:**
- Modify: `supercc/adapter/wecom/ws_client.py` — 添加 `reconnect()` 公开方法

- [ ] **Step 1: 读当前文件找到连接建立位置**

```bash
grep -n "def connect\|def _connect_loop\|start" supercc/adapter/wecom/ws_client.py
```

- [ ] **Step 2: 添加 reconnect() 方法**

在 `WeComWSClient` 类中添加：

```python
async def reconnect(self) -> None:
    """重新建立 WebSocket 连接（供 supervisor 调用）。"""
    logger.info("[WeComWS] Reconnecting...")
    self._running = False
    if self._ws:
        await self._ws.close()
    await asyncio.sleep(0.5)
    self._running = True
    asyncio.create_task(self._read_loop())
```

- [ ] **Step 3: 验证**

```bash
grep -n "reconnect\|_running" supercc/adapter/wecom/ws_client.py
```

- [ ] **Step 4: 提交**

```bash
git add supercc/adapter/wecom/ws_client.py
git commit -m "feat(wecom): add reconnect() method for restart supervisor"
```

---

## 任务 5: 集成 Restart Supervisor 到 start_bridge()

**Files:**
- Modify: `supercc/main.py` — `start_bridge()` 中集成 supervisor

- [ ] **Step 1: 读 start_bridge() 了解 Channel 启动位置**

确认：
1. FeishuWSClient 启动在 `ws_client.start()`（第 570 行）
2. WeComWSClient 启动在 `wecom_ws.start()`（第 547 行）
3. 两个都是 `daemon=True` 线程

- [ ] **Step 2: 在 start_bridge() 开头初始化 supervisor 和 store**

在 `# ── Phase 2` 前添加：

```python
# ── Phase 1: Channel Restart Supervisor ─────────────────────────────────────
from supercc.infra.backoff import CHANNEL_RESTART_POLICY, compute_backoff
from supercc.infra.channel_runtime import ChannelRuntimeStore

_channel_store = ChannelRuntimeStore()  # 全局单例

def _start_channel_supervisor(
    channel_id: str,
    start_fn,
    restart_fn,
):
    """启动一个 Channel 的 supervisor 线程。

    channel_id: 唯一标识（"feishu" | "wecom"）
    start_fn: 启动 Channel 的无参数函数（同步）
    restart_fn: 重新启动 Channel 的异步函数
    """
    def supervisor_loop():
        while True:
            try:
                # Channel 在主线程中启动
                start_fn()
                _channel_store.register(channel_id)
                logger.info(f"[Supervisor] {channel_id} started")
                # 等待 Channel 线程退出（通过共享的 running flag）
                # 这里用睡眠等待，实际由 Channel 的 daemon thread 监控
                while _channel_store.is_alive(channel_id):
                    if _channel_store.is_manually_stopped(channel_id):
                        logger.info(f"[Supervisor] {channel_id} manually stopped, exiting")
                        return
                    time.sleep(1)
            except Exception as e:
                logger.warning(f"[Supervisor] {channel_id} crashed: {e}")

            if _channel_store.is_manually_stopped(channel_id):
                return

            attempts = _channel_store.get_attempts(channel_id)
            if _channel_store.is_max_attempts_exceeded(channel_id):
                logger.error(f"[Supervisor] {channel_id} exceeded max restart attempts ({MAX_RESTART_ATTEMPTS}), giving up")
                return

            _channel_store.increment_attempts(channel_id)
            delay = compute_backoff(CHANNEL_RESTART_POLICY, _channel_store.get_attempts(channel_id))
            logger.info(f"[Supervisor] {channel_id} will restart in {delay}ms (attempt {_channel_store.get_attempts(channel_id)})")
            time.sleep(delay / 1000)

    import time
    t = threading.Thread(target=supervisor_loop, daemon=True, name=f"supervisor-{channel_id}")
    t.start()
```

- [ ] **Step 3: 重构 FeishuWSClient 启动为带 supervisor 版本**

将 `ws_client.start()` 调用替换为 supervisor 包装：

```python
# 5. 在 Supervisor 下启动 FeishuWSClient
def _start_feishu():
    ws_client.start()  # 阻塞

import threading
_feishu_running = True

def _feishu_monitor():
    global _feishu_running
    ws_client.start()
    _feishu_running = False

_feishu_thread = threading.Thread(target=_feishu_monitor, daemon=True, name="feishu-ws")
_feishu_thread.start()

# 注册到 store
_channel_store.register("feishu")
logger.info("[Phase2] FeishuWSClient started in monitored thread")

# 同时注册 WeCom 到 store（在 Phase3）
```

**注意**：由于现有架构中 WSClient.start() 是阻塞调用，更简洁的做法是利用 `ws_client.start()` 会在内部维护连接的特点，让 WSClient 自身在连接断开时触发 reconnect。参考任务 6。

- [ ] **Step 4: 提交**

```bash
git add supercc/main.py
git commit -m "feat(supervisor): integrate per-channel restart supervisor"
```

---

## 任务 6: WSClient 连接断开时自动触发 reconnect（最终方案）

**当前问题**：`ws_client.start()` 是阻塞调用，单线程架构下无法在 Channel 断开时自动重启。

**解决方案**：让每个 WSClient 的 `_read_loop` 在捕获到 `ConnectionClosed` 时调用 `reconnect()`，而不是直接退出。

- [ ] **Step 1: 修改 FeishuWSClient._read_loop() 添加重连逻辑**

在 `ws_client.py` 中找到 `_read_loop` 或等效循环：

```python
async def _read_loop(self):
    while self._running:
        try:
            async for msg in self._ws_client:
                await self._on_message(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("[FeishuWS] Connection closed, reconnecting...")
            await asyncio.sleep(5)
            await self._ws_client.connect()
        except Exception:
            logger.exception("[FeishuWS] Error in read loop")
```

- [ ] **Step 2: 同样修改 WeComWSClient._read_loop()**

```python
async def _read_loop(self):
    while self._running:
        try:
            msg = await asyncio.wait_for(self._ws.recv(), timeout=30)
            data = json.loads(msg)
            await self._handle_message(data)
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed:
            logger.warning("[WeComWS] Connection closed, reconnecting...")
            await asyncio.sleep(5)
            self._ws = await websockets.connect(self.WS_URL)
        except Exception:
            logger.exception("[WeComWS] Error in read loop")
```

- [ ] **Step 3: 验证**

```bash
grep -n "ConnectionClosed\|reconnect" supercc/adapter/feishu/ws_client.py
grep -n "ConnectionClosed\|reconnect" supercc/adapter/wecom/ws_client.py
```

- [ ] **Step 4: 提交**

```bash
git add supercc/adapter/feishu/ws_client.py supercc/adapter/wecom/ws_client.py
git commit -m "feat(ws): add auto-reconnect on connection closed"
```

---

## 任务 7: 验证端到端

- [ ] **Step 1: 启动 SuperCC，观察日志中是否有 supervisor 输出**

```bash
cd /Users/x/Desktop/创业项目/supercc && python -m supercc.main start
# 观察日志中是否出现:
# [Supervisor] feishu started
# [Supervisor] wecom started
```

- [ ] **Step 2: 检查 Channel 重启逻辑存在**

```bash
grep -rn "restart\|supervisor\|backoff" supercc/ --include="*.py" | grep -v test | grep -v __pycache__
```

---

## 任务 8: 更新 channel-restart-supervisor Skill

**Files:**
- Modify: `.supercc/skills/channel-restart-supervisor/SKILL.md`

- [ ] 更新"SuperCC 当前状态"一节，记录已实现的功能

---

## 检查清单

| 任务 | 状态 | 验证命令 |
|------|------|----------|
| backoff.py | ⏳ | `pytest tests/infra/test_backoff.py -v` |
| channel_runtime.py | ⏳ | `pytest tests/infra/test_channel_runtime.py -v` |
| FeishuWSClient reconnect() | ⏳ | `grep -n "reconnect" supercc/adapter/feishu/ws_client.py` |
| WeComWSClient reconnect() | ⏳ | `grep -n "reconnect" supercc/adapter/wecom/ws_client.py` |
| main.py supervisor 集成 | ⏳ | `grep -n "supervisor\|_channel_store" supercc/main.py` |
| WSClient auto-reconnect | ⏳ | `grep -n "ConnectionClosed" supercc/adapter/*/ws_client.py` |
| Skill 更新 | ⏳ | — |

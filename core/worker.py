"""Worker Pool with permanent chat_id binding and ClaudeIntegration management."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

from core.protocol import SessionKey

logger = logging.getLogger(__name__)


# ── Worker 状态 ────────────────────────────────────────────────────────────────

class WorkerState(str):
    IDLE = "idle"          # 空闲，等待任务
    BUSY = "busy"         # 处理中
    STOPPING = "stopping"  # 正在停止


@dataclass
class WorkerStats:
    """Worker 运行统计。"""
    session_id: str
    total_queries: int = 0
    total_cost: float = 0.0


# ── Worker ─────────────────────────────────────────────────────────────────────

@dataclass
class Worker:
    """
    绑定到特定 SessionKey 的 Worker 实例。

    每个 Worker 持有一个 ClaudeIntegration，在其生命周期内只处理同一个 chat_id 的消息。
    这保证了消息顺序和上下文连贯性。
    """
    key: SessionKey
    session_id: str                    # 关联的 session_id
    state: WorkerState = WorkerState.IDLE
    stats: WorkerStats = field(default_factory=lambda: WorkerStats(session_id=""))
    integration: Any = field(default=None)  # ClaudeIntegration 实例
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _current_task: asyncio.Task | None = None  # 当前执行中的 Task
    # SDK session 续接标志：_new_session_requested=True 时强制新建 session
    _is_first_session: bool = True
    _sdk_session_id: str | None = None  # 上次 query 返回的 SDK session ID，用于 resume

    def __post_init__(self):
        if self.stats.session_id == "":
            self.stats = WorkerStats(session_id=self.session_id)

    def reset_session(self) -> None:
        """重置会话标志，下次 query 强制新建 Claude SDK session。"""
        self._is_first_session = True
        if self.integration is not None:
            self.integration._new_session_requested = True
        logger.info(f"[Worker] Session reset for {self.key}")

    def stop(self) -> None:
        """打断当前正在执行的 query。"""
        if self.integration is not None:
            self.integration.stop_event.set()
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
        logger.info(f"[Worker] Stop requested for {self.key}")


# ── WorkerPool ────────────────────────────────────────────────────────────────

class WorkerPool:
    """
    按 chat_id 永久绑定 Worker 的池子。

    设计原则：
    - 同一个 SessionKey 的所有消息都路由到同一个 Worker
    - Worker 永不跨 chat_id 复用（永久绑定）
    - 核心进程内运行，不跨进程

    线程不安全，所有操作必须在 asyncio event loop 内进行。
    """

    def __init__(self):
        self._workers: dict[SessionKey, Worker] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: SessionKey, session_id: str, integration: Any) -> Worker:
        """
        获取或创建绑定到 key 的 Worker。

        同一个 key 永远返回同一个 Worker（永久绑定）。
        """
        async with self._lock:
            if key not in self._workers:
                worker = Worker(
                    key=key,
                    session_id=session_id,
                    integration=integration,
                )
                self._workers[key] = worker
                logger.info(f"[WorkerPool] Created worker for {key}")
            else:
                worker = self._workers[key]
            return worker

    async def release(self, key: SessionKey):
        """标记 Worker 为空闲（idle）。"""
        async with self._lock:
            if key in self._workers:
                self._workers[key].state = WorkerState.IDLE
                logger.debug(f"[WorkerPool] Worker released for {key}")

    async def get(self, key: SessionKey) -> Optional[Worker]:
        """获取已存在的 Worker，不存在则返回 None。"""
        async with self._lock:
            return self._workers.get(key)

    async def remove(self, key: SessionKey):
        """移除 Worker（通常在 session 删除时调用）。"""
        async with self._lock:
            if key in self._workers:
                del self._workers[key]
                logger.info(f"[WorkerPool] Removed worker for {key}")

    async def reset_session(self, key: SessionKey) -> bool:
        """重置指定 key 的 Worker 会话，下次 query 强制新建 session。"""
        async with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                return False
            worker.reset_session()
            return True

    async def stop(self, key: SessionKey) -> bool:
        """打断指定 key 的 Worker 的当前执行。"""
        async with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                return False
            worker.stop()
            return True

    async def list_workers(self) -> list[Worker]:
        """列出所有 Worker（调试用）。"""
        async with self._lock:
            return list(self._workers.values())

    async def stats(self) -> dict[str, Any]:
        """返回池子统计信息（调试用）。"""
        async with self._lock:
            return {
                "total_workers": len(self._workers),
                "idle": sum(1 for w in self._workers.values() if w.state == WorkerState.IDLE),
                "busy": sum(1 for w in self._workers.values() if w.state == WorkerState.BUSY),
            }

    async def execute(
        self,
        key: SessionKey,
        session_id: str,
        integration: Any,
        prompt: str,
        on_stream: Callable[[Any], Awaitable[None]] | None = None,
    ) -> tuple[str, float]:
        """
        为单个消息执行 Claude 查询。

        每个消息创建独立 asyncio.Task，支持并发。
        Worker 永久绑定 key，同一 key 的消息串行处理。
        """
        worker = await self.acquire(key, session_id, integration)
        async with worker._lock:
            worker.state = WorkerState.BUSY

        try:
            # 每个消息创建独立 task，在 worker 锁内等待完成
            task = asyncio.create_task(
                worker.integration.query(prompt=prompt, on_stream=on_stream)
            )
            worker._current_task = task
            result, sdk_sid, cost = await task
            # 记录 SDK session ID，供下次 query resume 使用
            if sdk_sid:
                worker._sdk_session_id = sdk_sid
            # /new 会重置 _is_first_session 和 _new_session_requested
            if worker._is_first_session:
                worker._is_first_session = False
            return result, cost
        finally:
            worker.state = WorkerState.IDLE
            worker._current_task = None
            await self.release(key)

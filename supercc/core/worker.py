"""Worker Pool with permanent chat_id binding and ClaudeIntegration management."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

from supercc.core.protocol import SessionKey

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

    每个 Worker 持有一组 ClaudeIntegration，在其生命周期内只处理同一个 chat_id 的消息。
    这保证了消息顺序和上下文连贯性。
    """
    key: SessionKey
    session_id: str                    # 关联的 session_id
    state: WorkerState = WorkerState.IDLE
    stats: WorkerStats = field(default_factory=lambda: WorkerStats(session_id=""))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _current_task: asyncio.Task | None = None  # 当前执行中的 Task
    _current_task_evolve: asyncio.Task | None = None  # evolve Task
    # SDK session 续接标志：_new_session_requested=True 时强制新建 session
    _is_first_session: bool = True
    _sdk_session_id: str | None = None  # 主对话上次 query 返回的 SDK session ID，用于 resume

    # ── 独立的 evolve 追踪 ──────────────────────────────────────────────
    _sdk_session_id_evolve: str | None = None  # evolve 专用 SDK session ID（与主对话完全独立）
    _evo_conversation_count: int = 0             # 距上次完整 evolve 的对话计数

    # ── 两个 ClaudeIntegration（acquire 初始化）──────────────────────────
    integration: Any = None           # 对话 + 定时任务（max_turns=50）
    integration_evolve: Any = None    # 自进化（max_turns=None）

    def __post_init__(self):
        if self.stats.session_id == "":
            self.stats = WorkerStats(session_id=self.session_id)

    def reset_session(self) -> None:
        """重置会话标志，下次 query 强制新建 Claude SDK session。"""
        self._is_first_session = True
        self._sdk_session_id = None
        self._sdk_session_id_evolve = None
        for integ in (self.integration, self.integration_evolve):
            if integ is not None:
                integ._new_session_requested = True
        logger.info(f"[Worker] Session reset for {self.key}")

    def stop(self) -> None:
        """打断当前正在执行的 query（主对话 + evolve）。"""
        for integ in (self.integration, self.integration_evolve):
            if integ is not None:
                integ.stop_event.set()
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
        if self._current_task_evolve is not None and not self._current_task_evolve.done():
            self._current_task_evolve.cancel()
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

    async def acquire(
        self,
        key: SessionKey,
        session_id: str,
        cli_path: str = "claude",
        approved_dir: str = "",
    ) -> Worker:
        """
        获取或创建绑定到 key 的 Worker，同时初始化三个 ClaudeIntegration（若尚未初始化）。

        同一个 key 永远返回同一个 Worker（永久绑定）。
        三个 integration 懒初始化，只在第一次 acquire 时创建。
        """
        async with self._lock:
            if key not in self._workers:
                from supercc.core.claude.integration import ClaudeIntegration
                worker = Worker(key=key, session_id=session_id)
                worker.integration = ClaudeIntegration(
                    cli_path=cli_path,
                    max_turns=50,
                    approved_directory=approved_dir,
                )
                worker.integration_evolve = ClaudeIntegration(
                    cli_path=cli_path,
                    max_turns=None,
                    approved_directory=approved_dir,
                    memory_only=True,
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
        prompt: str,
        system_prompt_append: str | None = None,
        cli_path: str = "claude",
        approved_dir: str = "",
        on_stream: Callable[[Any], Awaitable[None]] | None = None,
        on_start: Callable[[], Awaitable[None]] | None = None,
        sdk_session_id: str | None = None,
        for_evolve: bool = False,
    ) -> tuple[str, float]:
        """
        为单个消息执行 Claude 查询。

        每个消息创建独立 asyncio.Task，支持并发。
        Worker 永久绑定 key，同一 key 的消息串行处理。
        for_evolve=True 时使用 integration_evolve，stop 时能正确打断。
        sdk_session_id: 从 sessions DB 读取的 SDK session ID，首次 query 时用于 resume。
        """
        worker = await self.acquire(key, session_id, cli_path, approved_dir)
        async with worker._lock:
            worker.state = WorkerState.BUSY

        integ = worker.integration_evolve if for_evolve else worker.integration
        sdk_sid_ref = "_sdk_session_id_evolve" if for_evolve else "_sdk_session_id"
        current_task_ref = "_current_task_evolve" if for_evolve else "_current_task"

        try:
            resume = getattr(worker, sdk_sid_ref, None)
            # 重启后首次 query：从 DB 恢复 SDK session（非 /new 请求时才续接）
            if resume is None and sdk_session_id and not getattr(integ, '_new_session_requested', False):
                resume = sdk_session_id
            integ._init_options(
                system_prompt_append=system_prompt_append,
                continue_conversation=False,
                channel=key.platform,
                session_id=None,
                resume=resume,
            )

            task = asyncio.create_task(
                integ.query(prompt=prompt, on_stream=on_stream, on_start=on_start)
            )
            setattr(worker, current_task_ref, task)
            result, new_sid, cost = await task
            if new_sid:
                setattr(worker, sdk_sid_ref, new_sid)
            if not for_evolve:
                if worker._is_first_session:
                    worker._is_first_session = False
                worker._evo_conversation_count += 1
            return result, cost, new_sid
        finally:
            worker.state = WorkerState.IDLE
            setattr(worker, current_task_ref, None)
            await self.release(key)
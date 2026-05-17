"""线程安全的 Cron 消息投递队列。"""
import queue
from dataclasses import dataclass
from typing import Optional

from supercc.core.protocol import SessionKey


@dataclass
class CronDeliveryItem:
    """投递项：progress 或 result
    
    Core 只生成 Markdown 字符串，Plugin 自行决定如何渲染。
    """
    event: str  # "progress" | "result"
    session_key: SessionKey
    job_id: str
    job_name: str
    platform: str  # "feishu" | "wecom"
    content: str   # Markdown 格式内容
    error: Optional[str] = None  # 仅 result 使用


class CronDeliveryQueue:
    """线程安全的 FIFO 队列"""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()

    def put(self, item: CronDeliveryItem):
        self._queue.put(item)

    def get_all(self) -> list[CronDeliveryItem]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items


cron_delivery_queue = CronDeliveryQueue()

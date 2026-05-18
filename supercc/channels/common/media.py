"""跨平台的公共媒体工具。"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def save_bytes(path: str, data: bytes) -> None:
    """将字节写入文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    logger.info("[media] wrote %d bytes → %s", len(data), path)

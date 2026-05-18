"""跨平台的公共媒体工具。"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def save_bytes(path: str, data: bytes, prefix: str = "media") -> None:
    """将字节写入文件。

    Args:
        path: 文件保存路径
        data: 文件内容字节
        prefix: 日志前缀，如 "media" 或 "wecom"
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    logger.info("[%s] wrote %d bytes → %s", prefix, len(data), path)

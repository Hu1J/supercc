"""supercc logs 命令 — 查看 SuperCC 日志。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def view_logs(follow: bool = False, tail: int = 100) -> None:
    """查看日志文件内容。

    Args:
        follow: 是否实时跟踪（类似 tail -f）
        tail: 默认显示最后 N 行
    """
    from supercc.config import resolve_config_path

    _, data_dir = resolve_config_path()
    log_path = Path(data_dir) / "supercc.log"

    if not log_path.exists():
        print(f"日志文件不存在: {log_path}")
        print("SuperCC 尚未运行或日志文件在其他位置")
        return

    if follow:
        _tail_follow(log_path)
    else:
        _tail(log_path, tail)


def _tail(log_path: Path, n: int) -> None:
    """显示最后 n 行。"""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-n:]:
            print(line)
    except OSError as e:
        print(f"读取日志失败: {e}")


def _tail_follow(log_path: Path) -> None:
    """实时跟踪日志（类似 tail -f）。Ctrl+C 退出。"""
    print(f"实时跟踪日志: {log_path}")
    print("按 Ctrl+C 退出\n")

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # 先跳到文件末尾
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                print(line, end="")
    except KeyboardInterrupt:
        print("\n已退出实时跟踪")

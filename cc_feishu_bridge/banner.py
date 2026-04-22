from __future__ import annotations

"""Banner — terminal ASCII art and log file header.

This module provides ASCII art banners and formatting utilities.
"""

VERSION = "0.2.0"

import sys
from datetime import datetime
from pathlib import Path


RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"

TERMINAL_ART = """{RED}========================================{RESET}
  {RED}cc-feishu-bridge  v{version} 🚀{RESET}
  {GREEN}started at {timestamp}{RESET}
{RED}========================================{RESET}

"""


def print_banner(version: str) -> None:
    """Print the mini banner to terminal (sys.__stdout__)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        out = sys.__stdout__
        out.write(TERMINAL_ART.format(
            RED=RED, GREEN=GREEN, RESET=RESET,
            version=version, timestamp=timestamp,
        ))
        out.flush()
    except (OSError, IOError):
        pass  # Never crash on banner output


def write_log_banner(log_file: str, version: str) -> None:
    """Write mini banner to log file if it is empty or doesn't exist."""
    p = Path(log_file)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists() and p.stat().st_size > 0:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner = (
        f"{RED}========================================{RESET}\n"
        f"  {RED}cc-feishu-bridge  v{version}{RESET}\n"
        f"  {GREEN}started at {timestamp}{RESET}\n"
        f"{RED}========================================{RESET}\n\n"
    )
    with open(p, "a", encoding="utf-8") as f:
        f.write(banner)

"""Welcome banner, ASCII art, and version display for SuperCC.

Pure display functions with no state dependency.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


# =========================================================================
# ASCII Art & Branding
# =========================================================================

SUPERCC_LOGO = """[bold #FFD700]███████╗██╗  ██╗███████╗███████╗██████╗ ███████╗███████╗[/]
[bold #FFD700]██╔════╝██║  ██║██║  ██║██╔════╝██╔══██╗██╔════╝██╔════╝[/]
[#FFBF00]███████╗██║  ██║███████║█████╗  ██████╔╝██║     ██║     [/]
[#FFBF00]╚════██║██║  ██║██╔════╝██╔══╝  ██╔══██╗██║     ██║     [/]
[#CD7F32]███████║███████║██║     ███████╗██║  ██║███████╗███████╗[/]
[#CD7F32]╚══════╝╚══════╝╚═╝     ╚══════╝╚═╝  ╚═╝╚══════╝╚══════╝[/]"""




def _resolve_repo_dir() -> Optional[Path]:
    """Return the active SuperCC git checkout, or None if not a git install."""
    repo_dir = Path(__file__).parent.parent.resolve()
    return repo_dir if (repo_dir / ".git").exists() else None


def _git_short_hash(repo_dir: Path, rev: str) -> Optional[str]:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", rev],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def get_git_banner_state(repo_dir: Optional[Path] = None) -> Optional[dict]:
    """Return upstream/local git hashes for the startup banner."""
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        return None

    upstream = _git_short_hash(repo_dir, "origin/main")
    local = _git_short_hash(repo_dir, "HEAD")
    if not upstream or not local:
        return None

    ahead = 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            ahead = int((result.stdout or "0").strip() or "0")
    except Exception:
        ahead = 0

    return {"upstream": upstream, "local": local, "ahead": max(ahead, 0)}



# =========================================================================
# Non-blocking update check (stubbed)
# =========================================================================

_update_result: Optional[int] = None
_update_check_done = threading.Event()


def prefetch_update_check():
    """Kick off update check in a background daemon thread."""
    pass


def get_update_result(timeout: float = 0.5) -> Optional[int]:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result


# =========================================================================
# Welcome banner
# =========================================================================

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def _display_toolset_name(toolset_name: str) -> str:
    """Normalize internal/legacy toolset identifiers for banner display."""
    if not toolset_name:
        return "unknown"
    return (
        toolset_name[:-6]
        if toolset_name.endswith("_tools")
        else toolset_name
    )


def build_welcome_banner(console: Console, model: str, cwd: str,
                         tools: List[dict] = None,
                         enabled_toolsets: List[str] = None,
                         session_id: str = None,
                         get_toolset_for_tool=None,
                         context_length: int = None):
    """Build and print a welcome banner — dragon art + project path only."""
    import os
    tools = tools or []

    layout = Table.grid(padding=(0, 1))
    layout.add_column(justify="center")
    layout.add_row(SUPERCC_LOGO)
    layout.add_row(f"[dim #888]{cwd}[/]")

    title_color = "#FFD700"
    border_color = "#CD7F32"
    outer = Panel(
        layout,
        title=f"[bold {title_color}]{model} · Hu1J[/]",
        border_style=border_color,
        padding=(1, 2),
    )

    try:
        console.print()
        console.print(outer)
        console.print()
    except (OSError, IOError):
        pass


# ============================================================================
# Standalone print_banner / write_log_banner (used by main.py)
# ============================================================================

def print_banner(version: str) -> None:
    """Print the welcome banner — delegates to build_welcome_banner."""
    try:
        console = Console(highlight=False)
        import os
        cwd = os.getcwd()
        build_welcome_banner(
            console=console,
            model=f"龙王 SuperCC v{version}",
            cwd=cwd,
            tools=[],
            session_id=None,
        )
    except (OSError, IOError):
        pass


def write_log_banner(log_file: str, version: str) -> None:
    """Write banner header to log file if it is empty or doesn't exist."""
    p = Path(log_file)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists() and p.stat().st_size > 0:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner_lines = [
        "=" * 64,
        "  龙王 SuperCC",
        f"  v{version}",
        f"  启动时间: {timestamp}",
        "  自进化超级 AI · 越用越懂你",
        "=" * 64,
        "",
    ]
    banner = "\n".join(banner_lines)
    with open(p, "a", encoding="utf-8") as f:
        f.write(banner)

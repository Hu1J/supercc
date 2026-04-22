"""One-time migration from cc-feishu-bridge to SuperCC.

Handles two migration paths:
  - Home dir: ~/.cc-feishu-bridge/ → ~/.supercc/  (memories.db only)
  - Project dir: {cwd}/.cc-feishu-bridge/ → {cwd}/.supercc/  (cron_jobs, config.yaml, skills/)

This module is used ONCE, before supercc is installed. It is NOT imported by the bridge.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import yaml


class MigrationError(Exception):
    """Raised when migration fails."""


def migrate_home_dir() -> dict:
    """Migrate home directory data: ~/.cc-feishu-bridge/memories.db → ~/.supercc/memories.db.

    Returns a summary dict of what was done.
    """
    src_home = Path.home() / ".cc-feishu-bridge"
    dst_home = Path.home() / ".supercc"

    actions = []

    # Create destination dir
    dst_home.mkdir(parents=True, exist_ok=True)
    actions.append(f"created {dst_home}")

    # Copy memories.db
    src_db = src_home / "memories.db"
    if src_db.exists():
        shutil.copy2(src_db, dst_home / "memories.db")
        actions.append(f"copied {src_db} → {dst_home / 'memories.db'}")
    else:
        actions.append(f"skipped memories.db (not found at {src_db})")

    return {"home_migration": actions}


def migrate_project_dir(project_path: str) -> dict:
    """Migrate project directory data: {project}/.cc-feishu-bridge/ → {project}/.supercc/.

    Copies:
      - cron_jobs.json
      - config.yaml (storage section removed)
      - skills/ directory

    Returns a summary dict of what was done.
    """
    src_cc = Path(project_path) / ".cc-feishu-bridge"
    dst_cc = Path(project_path) / ".supercc"

    actions = []

    # Create destination dir
    dst_cc.mkdir(parents=True, exist_ok=True)
    actions.append(f"created {dst_cc}")

    # Copy cron_jobs.json
    src_cron = src_cc / "cron_jobs.json"
    if src_cron.exists():
        shutil.copy2(src_cron, dst_cc / "cron_jobs.json")
        actions.append(f"copied {src_cron}")
    else:
        actions.append("skipped cron_jobs.json (not found)")

    # Copy and clean config.yaml
    src_cfg = src_cc / "config.yaml"
    if src_cfg.exists():
        with open(src_cfg) as f:
            raw = yaml.safe_load(f)

        # Remove storage section (supercc stores db in home dir)
        if "storage" in raw:
            del raw["storage"]
            actions.append("removed storage section from config.yaml")

        dst_cfg = dst_cc / "config.yaml"
        with open(dst_cfg, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)
        actions.append(f"copied config.yaml (cleaned) → {dst_cfg}")
    else:
        actions.append("skipped config.yaml (not found)")

    # Copy skills/ directory
    src_skills = src_cc / "skills"
    dst_skills = dst_cc / "skills"
    if src_skills.exists() and src_skills.is_dir():
        if dst_skills.exists():
            actions.append(f"skipped skills/ (destination already exists)")
        else:
            shutil.copytree(src_skills, dst_skills)
            actions.append(f"copied {src_skills}/ → {dst_skills}/")
    else:
        actions.append("skipped skills/ (not found)")

    return {"project_migration": actions}


def run_migration(project_path: Optional[str] = None) -> dict:
    """Run all migrations.

    Args:
        project_path: Path to the project directory. Defaults to current working directory.

    Returns:
        A dict with migration results for home and project.
    """
    if project_path is None:
        project_path = os.getcwd()

    result = {}
    result.update(migrate_home_dir())
    result.update(migrate_project_dir(project_path))
    return result

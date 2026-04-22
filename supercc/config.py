"""Configuration loading and validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict

import yaml

# sessions.db 固定放在家目录下，不同项目通过 session.project_path 区分
SESSIONS_DB_PATH = str(Path.home() / ".supercc" / "sessions.db")


@dataclass
class GroupConfigEntry:
    """Per-group configuration for group chat access control."""
    enabled: bool = True              # 是否启用该群
    require_mention: bool = True     # 是否必须 @CC 才响应
    allow_from: list[str] = field(default_factory=list)  # 白名单 open_id（空=不限）


# Type alias for group config dict: group_id -> GroupConfigEntry
GroupConfig = Dict[str, GroupConfigEntry]


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    bot_name: str = "Claude"
    bot_open_id: str = ""        # 机器人的 open_id，用于检测群聊 @CC
    domain: str = "feishu"
    groups: dict = field(default_factory=dict)  # group_id -> GroupConfigEntry dict


@dataclass
class AuthConfig:
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class ClaudeConfig:
    cli_path: str = "claude"
    max_turns: int = 50
    approved_directory: str = str(Path.home())


@dataclass
class StorageConfig:
    db_path: str = SESSIONS_DB_PATH


@dataclass
class SkillNudgeConfig:
    enabled: bool = True
    interval: int = 10
    current_user: str = ""  # matched against skill author for auto-evolve


@dataclass
class Config:
    feishu: FeishuConfig
    auth: AuthConfig
    claude: ClaudeConfig
    storage: StorageConfig
    skill_nudge: SkillNudgeConfig = field(default_factory=SkillNudgeConfig)
    data_dir: str = ""
    bypass_accepted: bool = False


def _upgrade_config(path: str) -> None:
    """Auto-upgrade config.yaml: add proactive section if missing, remove stale server section."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    changed = False

    # Remove stale server section (deprecated in v0.2.3)
    if "server" in raw:
        del raw["server"]
        changed = True

    if changed:
        with open(path, "w") as f:
            yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_config(path: str, data_dir: str = "") -> Config:
    """Load and validate configuration from YAML file."""
    _upgrade_config(path)
    with open(path) as f:
        raw = yaml.safe_load(f)

    # Deserialize groups: convert raw dicts to GroupConfigEntry objects
    # Filter out unknown fields to tolerate future config additions gracefully.
    _known_group_keys = {"enabled", "require_mention", "allow_from"}
    raw_groups = raw.get("feishu", {}).get("groups", {})
    groups = {
        gid: GroupConfigEntry(**{k: v for k, v in gentry.items() if k in _known_group_keys})
        for gid, gentry in raw_groups.items()
    }

    feishu_raw = raw.get("feishu", {}).copy()
    feishu_raw["groups"] = groups
    feishu_cfg = FeishuConfig(**feishu_raw)

    return Config(
        feishu=feishu_cfg,
        auth=AuthConfig(**raw.get("auth", {})),
        claude=ClaudeConfig(**raw.get("claude", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        skill_nudge=SkillNudgeConfig(**raw.get("skill_nudge", {})),
        data_dir=data_dir,
        bypass_accepted=raw.get("bypass_accepted", False),
    )


def save_config(path: str, feishu_app_id: str, feishu_app_secret: str,
                domain: str, bot_name: str,
                bot_open_id: str,
                allowed_users: list[str],
                claude_cli_path: str, claude_max_turns: int,
                claude_approved_directory: str,
                storage_db_path: str,
                bypass_accepted: bool = False,
                groups: dict | None = None) -> None:
    """Save a complete config to a YAML file."""
    feishu_cfg = {
        "app_id": feishu_app_id,
        "app_secret": feishu_app_secret,
        "bot_name": bot_name,
        "bot_open_id": bot_open_id,
        "domain": domain,
    }
    if groups:
        feishu_cfg["groups"] = {
            gid: {
                "enabled": entry.enabled,
                "require_mention": entry.require_mention,
                "allow_from": entry.allow_from,
            }
            for gid, entry in groups.items()
        }
    config = {
        "feishu": feishu_cfg,
        "auth": {
            "allowed_users": allowed_users,
        },
        "claude": {
            "cli_path": claude_cli_path,
            "max_turns": claude_max_turns,
            "approved_directory": claude_approved_directory,
        },
        "storage": {
            "db_path": storage_db_path,
        },
        "bypass_accepted": bypass_accepted,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def register_group_config(config_path: str, group_id: str, entry: GroupConfigEntry | None = None) -> bool:
    """Auto-register a group in the config file. Creates default entry if none provided.

    Returns True if the group was newly registered, False if it already existed.
    """
    if entry is None:
        entry = GroupConfigEntry()

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    feishu_section = raw.get("feishu")
    if feishu_section is None:
        # Config file has no feishu section — create it with a groups subkey
        raw["feishu"] = {"groups": {}}
        feishu_section = raw["feishu"]

    groups = feishu_section.get("groups", {})
    if group_id in groups:
        return False  # already registered

    groups[group_id] = {
        "enabled": entry.enabled,
        "require_mention": entry.require_mention,
        "allow_from": entry.allow_from,
    }
    feishu_section["groups"] = groups

    with open(config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)

    return True  # newly registered


def accept_bypass_warning(config_path: str) -> None:
    """Record that the bypass permissions risk warning has been accepted."""
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    raw["bypass_accepted"] = True
    with open(config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)


README_CONTENT = """# .supercc

This directory is created automatically by `supercc` and contains the config for this project instance.

## Contents

- `config.yaml` — Bot credentials and configuration
- `skills/` — Private skills for this project
- `cron_jobs.json` — Cron job definitions

Note: sessions.db and memories.db live in ~/.supercc/ (home dir, shared across projects).
Other data (cron, logs, skills, media, pid) lives in {project}/.supercc/.

## Git Ignore

This directory is gitignored. It should never be committed.

"""


def resolve_config_path() -> tuple[str, str]:
    """Resolve config and data directories.

    Config lives in project dir: {cwd}/.supercc/config.yaml
    Data (sessions, logs, PID) also lives in {cwd}/.supercc/.

    Auto-creates both directories if not found.
    """
    import os
    cwd = os.getcwd()
    cfg_dir = Path(cwd).resolve() / ".supercc"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.touch(exist_ok=True)
    readme_path = cfg_dir / "README.md"
    readme_path.write_text(README_CONTENT, errors="replace")

    data_dir = str(cfg_dir)
    return (str(cfg_path), data_dir)

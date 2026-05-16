"""Skill metadata dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SkillMeta:
    """Metadata for a discovered skill."""

    name: str
    description: str
    source: str  # e.g. "skills.sh", "github", "hermes-index", "local"
    identifier: str  # e.g. "claude-code/skills/shell-commands"
    trust_level: str = "medium"  # "high" | "medium" | "low"
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # source-specific data

    def __post_init__(self):
        if self.trust_level not in ("high", "medium", "community"):
            self.trust_level = "medium"

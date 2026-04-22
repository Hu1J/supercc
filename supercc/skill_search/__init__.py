"""Skill search — multi-source parallel search registry.

Exposes a single get_skill_search_registry() singleton with all Hermes-style
sources registered: SkillsShSource, GitHubSource, HermesIndexSource,
ClawHubSource, ClaudeMarketplaceSource, LobeHubSource, WellKnownSkillSource.
"""
from __future__ import annotations

import threading
from typing import Optional

from supercc.skill_search.registry import SkillSearchRegistry
from supercc.skill_search.sources import (
    SkillsShSource,
    GitHubSource,
    HermesIndexSource,
    ClawHubSource,
    ClaudeMarketplaceSource,
    LobeHubSource,
    WellKnownSkillSource,
    # SkillHubSource: skillhub.cn API requires auth (401), no public API available
)

_registry: Optional[SkillSearchRegistry] = None
_registry_lock = threading.Lock()


def get_skill_search_registry() -> SkillSearchRegistry:
    """Get or create the singleton SkillSearchRegistry with all sources registered."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SkillSearchRegistry(timeout=30.0)
                # Register all Hermes-style sources
                _registry.register(SkillsShSource())
                _registry.register(GitHubSource())
                _registry.register(HermesIndexSource())
                _registry.register(ClawHubSource())
                _registry.register(ClaudeMarketplaceSource())
                _registry.register(LobeHubSource())
                _registry.register(WellKnownSkillSource())
                # SkillHubSource: skillhub.cn API requires auth (401), no public API available
    return _registry

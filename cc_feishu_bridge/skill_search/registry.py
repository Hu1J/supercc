"""Parallel multi-source skill search registry."""
from __future__ import annotations

import asyncio
import logging

from cc_feishu_bridge.skill_search.models import SkillMeta
from cc_feishu_bridge.skill_search.sources import (
    SkillSource,
    SkillsShSource,
    GitHubSource,
    HermesIndexSource,
    ClawHubSource,
    ClaudeMarketplaceSource,
    LobeHubSource,
    WellKnownSkillSource,
)

logger = logging.getLogger(__name__)


class SkillSearchRegistry:
    """Registry that manages multiple SkillSource and parallel search."""

    def __init__(self, timeout: float = 30.0):
        self._sources: list[SkillSource] = []
        self.timeout = timeout

    def register(self, source: SkillSource):
        self._sources.append(source)

    def _dedup_and_sort(self, results: list[SkillMeta]) -> list[SkillMeta]:
        """Deduplicate by identifier, keeping highest trust_level."""
        seen: dict[str, SkillMeta] = {}
        for meta in results:
            key = meta.identifier.lower()
            if key not in seen or self._trust_better(meta, seen[key]):
                seen[key] = meta

        # Sort by trust level (high first)
        sorted_results = sorted(seen.values(), key=lambda m: self._trust_priority(m.trust_level))
        return sorted_results

    def _trust_priority(self, level: str) -> int:
        """Get priority value for trust level (lower = higher priority)."""
        from cc_feishu_bridge.skill_search.sources import TRUST_PRIORITY

        return TRUST_PRIORITY.get(level, 1)

    def _trust_better(self, a: SkillMeta, b: SkillMeta) -> bool:
        """Return True if a has better trust level than b."""
        return self._trust_priority(a.trust_level) < self._trust_priority(b.trust_level)

    async def search_all(self, query: str, limit_per_source: int = 3) -> list[SkillMeta]:
        """Search all sources in parallel."""

        async def search_one(source: SkillSource) -> list[SkillMeta]:
            try:
                return await asyncio.wait_for(
                    source.search(query, limit_per_source),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[SkillSearchRegistry] {source.name} timed out")
                return []
            except Exception as e:
                logger.warning(f"[SkillSearchRegistry] {source.name} error: {e}")
                return []

        tasks = [search_one(s) for s in self._sources]
        results = await asyncio.gather(*tasks)
        flat = [item for sublist in results for item in sublist]
        return self._dedup_and_sort(flat)

    async def close(self):
        """Close all registered sources."""
        for source in self._sources:
            try:
                await source.close()
            except Exception:
                pass

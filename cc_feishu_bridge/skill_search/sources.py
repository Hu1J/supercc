"""SkillSource ABC and multi-source implementations (Hermes-style)."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from cc_feishu_bridge.skill_search.models import SkillMeta

logger = logging.getLogger(__name__)

# Trust level priority (high > medium > community)
TRUST_PRIORITY = {"high": 0, "medium": 1, "community": 2}


class SkillSource(ABC):
    """Abstract base class for skill sources."""

    name: str  # human-readable source name

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        """Semantic search."""
        ...

    async def close(self):
        """Close the source's HTTP client. Subclasses should override if needed."""
        pass


# ─── Skills.sh Source ──────────────────────────────────────────────────────────


class SkillsShSource(SkillSource):
    """Search via skills.sh aggregation API.

    Ref: Hermes skills_hub.py - SkillsShSource
    API: GET https://skills.sh/api/search?q={query}&limit={limit}
    """

    name = "skills.sh"
    BASE_URL = "https://skills.sh"

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        try:
            resp = await self._client.get(
                f"{self.BASE_URL}/api/search",
                params={"q": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills", [])
            return [
                SkillMeta(
                    name=s.get("name", s.get("skillId", "")),
                    description=s.get("description", ""),
                    source=self.name,
                    identifier=s.get("id", s.get("name", "")),
                    trust_level=s.get("trust_level", "medium"),
                    tags=s.get("tags", []),
                    extra=s,
                )
                for s in skills
                if s.get("name") or s.get("skillId")
            ]
        except Exception as e:
            logger.warning(f"[SkillsShSource] search error: {e}")
            return []

    async def close(self):
        await self._client.aclose()


# ─── GitHub Source ─────────────────────────────────────────────────────────────


class GitHubSource(SkillSource):
    """Search GitHub repositories for skills.

    Ref: Hermes skills_hub.py - GitHubSource
    Default taps: openai/skills, anthropics/skills, VoltAgent/awesome-agent-skills
    """

    name = "github"
    DEFAULT_TAPS = [
        {"repo": "openai/skills", "path": "skills/"},
        {"repo": "anthropics/skills", "path": "skills/"},
        {"repo": "VoltAgent/awesome-agent-skills", "path": "skills/"},
    ]
    MAX_RETRIES = 1  # 一次 retry，应对短暂限流

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)

    async def _get_with_retry(self, url: str, **kwargs) -> httpx.Response:
        """GET with one retry on failure."""
        headers = kwargs.pop("headers", {})
        headers.setdefault("Accept", "application/vnd.github.v3+json")
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = await self._client.get(url, headers=headers, **kwargs)
                if resp.status_code == 403 and attempt < self.MAX_RETRIES:
                    logger.warning(f"[GitHubSource] 403, retrying in 1s...")
                    await asyncio.sleep(1)
                    continue
                return resp
            except Exception:
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(0.5)
                    continue
                raise

    async def _list_directory(self, repo: str, path: str) -> list[dict]:
        try:
            resp = await self._get_with_retry(
                f"https://api.github.com/repos/{repo}/contents/{path}",
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[GitHubSource] failed to list {repo}/{path}: {e}")
            return []

    async def _fetch_file_content(self, repo: str, path: str) -> Optional[str]:
        try:
            resp = await self._client.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={"Accept": "application/vnd.github.v3.raw"},
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.warning(f"[GitHubSource] failed to fetch {repo}/{path}: {e}")
            return None

    def _parse_skill_content(self, content: str) -> tuple[str, list[str]]:
        """Parse SKILL.md content for description and tags.

        Supports:
        - description:/tags: inline (Hermes format)
        - YAML frontmatter ---...--- (full YAML frontmatter)
        """
        description = ""
        tags = []

        content = content.strip()
        if content.startswith("---"):
            try:
                import yaml

                end = content.index("---", 3)
                frontmatter = yaml.safe_load(content[3:end])
                if frontmatter:
                    description = str(frontmatter.get("description", ""))
                    raw_tags = frontmatter.get("tags", [])
                    if isinstance(raw_tags, list):
                        tags = [str(t) for t in raw_tags]
                content = content[end + 3 :]
            except Exception:
                pass

        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("description:") and not description:
                description = line.split(":", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("tags:") and not tags:
                tags_str = line.split(":", 1)[1].strip().strip("[]")
                tags = [t.strip().strip("'").strip('"') for t in tags_str.split(",") if t.strip()]

        return description, tags

    async def _parse_skill_meta(self, repo: str, skill_name: str, skill_path: str) -> Optional[SkillMeta]:
        """Parse SKILL.md to extract metadata."""
        content = await self._fetch_file_content(repo, f"{skill_path}{skill_name}/SKILL.md")
        if not content:
            content = await self._fetch_file_content(repo, f"{skill_name}/SKILL.md")
        if not content:
            return None

        description, tags = self._parse_skill_content(content)

        return SkillMeta(
            name=skill_name,
            description=description[:200] if description else "",
            source=self.name,
            identifier=f"{repo}/{skill_path}{skill_name}",
            trust_level="medium",
            tags=tags,
            extra={"repo": repo, "path": skill_path},
        )

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        results = []
        query_lower = query.lower()

        for tap in self.DEFAULT_TAPS:
            repo = tap["repo"]
            path = tap["path"]

            try:
                entries = await self._list_directory(repo, path)
                for entry in entries:
                    if entry.get("type") != "dir":
                        continue
                    skill_name = entry.get("name", "")
                    if not skill_name.startswith("."):
                        meta = await self._parse_skill_meta(repo, skill_name, path)
                        if meta and (
                            query_lower in skill_name.lower() or query_lower in meta.description.lower()
                        ):
                            results.append(meta)
                            if len(results) >= limit:
                                return results
            except Exception as e:
                logger.warning(f"[GitHubSource] search error for {repo}: {e}")

        return results

    async def close(self):
        await self._client.aclose()


# ─── Hermes Index Source ───────────────────────────────────────────────────────


class HermesIndexSource(SkillSource):
    """Search via Hermes集中式 JSON 索引.

    Ref: Hermes skills_hub.py - HermesIndexSource
    URL: https://hermes-agent.nousresearch.com/docs/api/skills-index.json
    Cache TTL: 6 hours
    """

    name = "hermes-index"
    INDEX_URL = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
        self._cache: Optional[dict] = None
        self._lock = asyncio.Lock()

    async def _get_index(self) -> dict:
        """Get cached index (thread-safe)."""
        if self._cache is None:
            async with self._lock:
                if self._cache is None:  # double-check after acquiring lock
                    try:
                        resp = await self._client.get(self.INDEX_URL)
                        resp.raise_for_status()
                        self._cache = resp.json()
                    except Exception as e:
                        logger.warning(f"[HermesIndexSource] failed to fetch index: {e}")
                        self._cache = {"skills": []}
        return self._cache

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        index = await self._get_index()
        query_lower = query.lower()
        results = []

        for s in index.get("skills", []):
            name = s.get("name", "")
            desc = s.get("description", "").lower()
            if query_lower in name.lower() or query_lower in desc:
                results.append(
                    SkillMeta(
                        name=name,
                        description=s.get("description", ""),
                        source=self.name,
                        identifier=s.get("identifier", name),
                        trust_level=s.get("trust_level", "medium"),
                        tags=s.get("tags", []),
                        extra=s,
                    )
                )
                if len(results) >= limit:
                    break

        return results

    async def close(self):
        await self._client.aclose()


# ─── ClawHub Source ────────────────────────────────────────────────────────────


class ClawHubSource(SkillSource):
    """Search clawhub.ai.

    Ref: Hermes skills_hub.py - ClawHubSource
    API Base: https://clawhub.ai/api/v1
    Note: Always trust_level="community" per Hermes (vetting insufficient)
    """

    name = "clawhub"
    BASE_URL = "https://clawhub.ai/api/v1"

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        try:
            resp = await self._client.get(
                f"{self.BASE_URL}/skills",
                params={"search": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            return [
                SkillMeta(
                    name=s.get("displayName", s.get("slug", "")),
                    description=s.get("summary", ""),
                    source=self.name,
                    identifier=f"clawhub/{s.get('slug', '')}",
                    trust_level="community",  # Per Hermes: vetting insufficient
                    tags=s.get("tags", []),
                    extra=s,
                )
                for s in items
                if s.get("slug")
            ]
        except Exception as e:
            logger.warning(f"[ClawHubSource] search error: {e}")
            return []

    async def close(self):
        await self._client.aclose()


# NOTE: SkillHub (skillhub.cn) has an API at https://api.skillhub.cn/api/v1/skills
# but it requires authentication (401). Removed until public API is available.


# ─── Claude Marketplace Source ─────────────────────────────────────────────────


class ClaudeMarketplaceSource(SkillSource):
    """Search Claude marketplace.json from GitHub repos.

    Ref: Hermes skills_hub.py - ClaudeMarketplaceSource
    Marketplace repos: anthropics/skills/.claude-plugin/marketplace.json
    """

    name = "claude-marketplace"
    MARKETPLACE_REPOS = [
        "anthropics/skills",
    ]

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
        self._cache: dict[str, dict] = {}

    async def _fetch_marketplace(self, repo: str) -> dict:
        """Fetch marketplace.json from a repo."""
        if repo in self._cache:
            return self._cache[repo]

        try:
            resp = await self._client.get(
                f"https://api.github.com/repos/{repo}/contents/.claude-plugin/marketplace.json",
                headers={"Accept": "application/vnd.github.v3.raw"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._cache[repo] = data.get("plugins", [])
            return self._cache[repo]
        except Exception as e:
            logger.warning(f"[ClaudeMarketplaceSource] failed to fetch {repo}: {e}")
            return []

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        results = []
        query_lower = query.lower()

        for repo in self.MARKETPLACE_REPOS:
            plugins = await self._fetch_marketplace(repo)
            for p in plugins:
                name = p.get("name", "")
                desc = p.get("description", "").lower()
                if query_lower in name.lower() or query_lower in desc:
                    results.append(
                        SkillMeta(
                            name=name,
                            description=p.get("description", ""),
                            source=self.name,
                            identifier=f"{repo}/{name}",
                            trust_level="high",  # Official Claude repos are high trust
                            tags=p.get("tags", []),
                            extra={"repo": repo, "source_path": p.get("source", "")},
                        )
                    )
                    if len(results) >= limit:
                        return results

        return results

    async def close(self):
        await self._client.aclose()


# ─── LobeHub Source ────────────────────────────────────────────────────────────


class LobeHubSource(SkillSource):
    """Search lobehub.com agents.

    Ref: Hermes skills_hub.py - LobeHubSource
    Index URL: https://chat-agents.lobehub.com/index.json
    """

    name = "lobehub"
    INDEX_URL = "https://chat-agents.lobehub.com/index.json"

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)
        self._cache: Optional[list] = None
        self._lock = asyncio.Lock()

    async def _get_index(self) -> list:
        """Get agents index (thread-safe)."""
        if self._cache is None:
            async with self._lock:
                if self._cache is None:  # double-check after acquiring lock
                    try:
                        resp = await self._client.get(self.INDEX_URL)
                        resp.raise_for_status()
                        data = resp.json()
                        self._cache = data.get("agents", [])
                    except Exception as e:
                        logger.warning(f"[LobeHubSource] failed to fetch index: {e}")
                        self._cache = []
        return self._cache

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        index = await self._get_index()
        query_lower = query.lower()
        results = []

        for agent in index:
            meta = agent.get("meta", {})
            name = meta.get("title", agent.get("identifier", ""))
            desc = meta.get("description", "").lower()
            if query_lower in name.lower() or query_lower in desc:
                results.append(
                    SkillMeta(
                        name=name,
                        description=meta.get("description", ""),
                        source=self.name,
                        identifier=agent.get("identifier", name),
                        trust_level="medium",
                        tags=meta.get("tags", []),
                        extra=agent,
                    )
                )
                if len(results) >= limit:
                    break

        return results

    async def close(self):
        await self._client.aclose()


# ─── Well-Known Skills Source ──────────────────────────────────────────────────


class WellKnownSkillSource(SkillSource):
    """Search /.well-known/skills/index.json.

    Ref: Hermes skills_hub.py - WellKnownSkillSource
    Pattern: GET https://{domain}/.well-known/skills/index.json
    """

    name = "well-known"
    WELL_KNOWN_DOMAINS = [
        "https://skills.sh",
        "https://agent.skills.sh",
    ]

    def __init__(self, timeout: float = 10.0, follow_redirects: bool = True):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)

    async def _fetch_index(self, base_url: str) -> list:
        """Fetch well-known index from a domain."""
        url = f"{base_url.rstrip('/')}/.well-known/skills/index.json"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("skills", [])
        except Exception:
            return []

    async def search(self, query: str, limit: int = 5) -> list[SkillMeta]:
        results = []
        query_lower = query.lower()

        for base_url in self.WELL_KNOWN_DOMAINS:
            if len(results) >= limit:
                break
            skills = await self._fetch_index(base_url)
            for s in skills:
                name = s.get("name", "")
                desc = s.get("description", "").lower()
                if query_lower in name.lower() or query_lower in desc:
                    results.append(
                        SkillMeta(
                            name=name,
                            description=s.get("description", ""),
                            source=f"{self.name}-{base_url}",
                            identifier=s.get("identifier", name),
                            trust_level=s.get("trust_level", "medium"),
                            tags=s.get("tags", []),
                            extra=s,
                        )
                    )
                    if len(results) >= limit:
                        break

        return results

    async def close(self):
        await self._client.aclose()

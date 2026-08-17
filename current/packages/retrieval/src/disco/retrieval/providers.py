"""Discovery & extraction provider protocols — retrieval-grounding-contract.md §2.

Two distinct slots (principle 2): *discovery* (query → candidate URLs) and
*extraction* (URL → clean content with provenance). SearXNG/Firecrawl are the
self-hosted defaults (principle 3); the live HTTP implementations are deferred
([VERIFY] owned-instance endpoints) — the logic is testable against fakes first.
Key-bearing providers are reached via the orchestrator capability (principle 7).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ExtractedDoc, SearchHit


@runtime_checkable
class SearchProvider(Protocol):
    """[CONTRACT] Discovery: a query -> candidate URLs."""

    name: str  # "searxng" | "serper" | "brave" | "exa" | "tavily" | ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]: ...


@runtime_checkable
class ExtractionProvider(Protocol):
    """[CONTRACT] Extraction: a URL -> ExtractedDoc (content + provenance)."""

    name: str  # "firecrawl" | "trafilatura" | "jina" | ...

    async def extract(self, url: str) -> ExtractedDoc: ...

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]: ...

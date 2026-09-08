"""Record/replay wrappers for the retrieval seams (search + extraction) — the
network-bound, nondeterministic providers. The bundled encoders (rerank/embed/
nli via fastembed) are deterministic CPU, so they run live even under replay and
need no cassette.

Recording wraps the REAL provider, calls through, and records the verbatim
`model_dump()`. Replay reconstructs via `model_validate()` — same contract, no
network. Both conform to the SearchProvider / ExtractionProvider protocols so
they drop straight into `build_live_retrieval`'s slots.
"""

from __future__ import annotations

from disco.retrieval.models import ExtractedDoc, SearchHit
from disco.retrieval.providers import SearchProvider

from .cassette import Cassette, CassetteMiss

# ---- search -----------------------------------------------------------------


def _search_payload(
    query: str, limit: int, domains_deny, domains_allow=None, time_filter=None
) -> dict:
    payload = {"query": query, "limit": limit, "deny": sorted(domains_deny or [])}
    # Preserve unfiltered legacy keys while distinguishing every scoped request.
    if domains_allow:
        payload["allow"] = sorted(domains_allow)
    if time_filter is not None:
        payload["time_filter"] = time_filter
    return payload


class RecordingSearchProvider:
    """Wraps a real SearchProvider; records each result list verbatim."""

    def __init__(self, inner: SearchProvider, cassette: Cassette):
        self._inner = inner
        self._cas = cassette
        self.name = inner.name
        cassette.record("search.provider", {}, {"name": self.name})

    async def search(
        self, query, *, limit=10, domains_allow=None, domains_deny=None, time_filter=None
    ):
        hits = await self._inner.search(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        self._cas.record_next(
            "search",
            _search_payload(query, limit, domains_deny, domains_allow, time_filter),
            [h.model_dump(mode="json") for h in hits],
        )
        return hits


class ReplaySearchProvider:
    """Returns the recorded SearchHits for a matching query — no network."""

    name = "replay-search"

    def __init__(self, cassette: Cassette):
        self._cas = cassette
        try:
            self.name = cassette.lookup("search.provider", {})["name"]
        except CassetteMiss:
            self.name = "replay-search"

    async def search(
        self, query, *, limit=10, domains_allow=None, domains_deny=None, time_filter=None
    ):
        rows = self._cas.lookup_next(
            "search", _search_payload(query, limit, domains_deny, domains_allow, time_filter)
        )
        return [SearchHit.model_validate(r) for r in rows]


# ---- extraction -------------------------------------------------------------


class RecordingExtractionProvider:
    """Wraps a real ExtractionProvider; records each ExtractedDoc verbatim (keyed
    per-url, so extract_many is just N recorded extracts)."""

    def __init__(self, inner, cassette: Cassette):
        self._inner = inner
        self._cas = cassette
        self.name = getattr(inner, "name", "extraction")

    async def extract(self, url: str) -> ExtractedDoc:
        doc = await self._inner.extract(url)
        self._cas.record_next("extract", {"url": url}, doc.model_dump(mode="json"))
        return doc

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        docs = await self._inner.extract_many(urls)
        for url, doc in zip(urls, docs, strict=True):
            self._cas.record_next("extract", {"url": url}, doc.model_dump(mode="json"))
        return docs


class ReplayExtractionProvider:
    name = "replay-extraction"

    def __init__(self, cassette: Cassette):
        self._cas = cassette

    async def extract(self, url: str) -> ExtractedDoc:
        return ExtractedDoc.model_validate(self._cas.lookup_next("extract", {"url": url}))

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        return [await self.extract(u) for u in urls]

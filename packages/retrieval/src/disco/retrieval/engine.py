"""The retrieval engine — retrieval-grounding-contract.md §3.

Orchestrates the quality pipeline: one search of the query as written → broad
retrieval → extraction with provenance → rerank → return. Passages come ONLY
from extracted docs / corpora — never from a SearchHit snippet (principle 4
"never trust the snippet").

A complete HTTP(S) URL is read directly through the same extraction pipeline;
its trace records a direct read and no issued search queries. Search terms
continue to reach the search provider unchanged.

The engine does not author queries. `req.query` is written by the caller — on
the deep-research path, by the model, under a page of instructions about what a
good query is — and it reaches the provider byte for byte, once. The host used
to rewrite it through an LLM (one paraphrase at `standard`, four at `deep`) and
to keyword-compress the result; measured over 16 recorded runs that replaced the
model's query in 98% of searches, multiplied every `deep` query into four
near-duplicate searches (median pairwise Jaccard 0.85 — above the threshold at
which the loop refuses the MODEL for repeating itself), and dropped the `site:`
operator or the year the model wrote in a quarter of the queries carrying one.
Breadth now comes from `discover_limit` — keeping what one search returns — not
from issuing the same question four ways.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._direct_source import _direct_discovery, _direct_trace, _source_url
from ._extraction_text import reject_challenge_page
from ._transport_retry import (
    RATE_WAIT_KEY,
    _extraction_error_class,
    acquire_search_slot,
    search_with_degradation_retry,
)
from .models import ExtractedDoc, Passage, RetrievalRequest, RetrievalResult, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, Reranker, deduplicate_search_hits, distinct_source_passages
from .url_policy import source_url_key

# Default cap on URLs sent to extraction per retrieve() (cost bound; [INTERIOR]).
_CANDIDATE_CAP = 20

# Retrieval diagnostics are persisted in Deep Research checkpoints and may be
# sent to the UI. Keep them useful for reproductions without turning them into
# a second copy of provider responses (or leaking credentials in URLs).
_TRACE_QUERY_CHARS = 500
_TRACE_TITLE_CHARS = 240
_TRACE_URL_CHARS = 500
_TRACE_ENGINE_CHARS = 80
_TRACE_MAX_HITS_PER_QUERY = 20
_TRACE_MAX_DIAGNOSTIC_ITEMS = 20
_TRACE_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "code",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_TRACE_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:access[_-]?token|api[_-]?key|auth|password|secret|signature|token)\b\s*[:=]\s*)[^\s&,;]+"
)


def _trace_text(value: object, limit: int) -> str:
    """Return bounded single-line diagnostic text."""

    text = " ".join(str(value or "").split())
    return text[:limit]


def _trace_url(value: object) -> str:
    """Redact credential-shaped query parameters and bound a diagnostic URL."""

    raw = _trace_text(value, _TRACE_URL_CHARS * 2)
    try:
        parts = urlsplit(raw)
        query = [
            (key, "[REDACTED]" if key.casefold() in _TRACE_SECRET_KEYS else val[:120])
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return _trace_text(
            urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")),
            _TRACE_URL_CHARS,
        )
    except ValueError:
        return _trace_text(raw, _TRACE_URL_CHARS)


def _trace_snippet(value: object) -> dict[str, object] | None:
    """Bound an untrusted provider snippet and redact obvious credentials."""

    text = _trace_text(value, 300)
    if not text:
        return None
    return {
        "text": _TRACE_SECRET_VALUE.sub(r"\1[REDACTED]", text),
        "untrusted": True,
    }


def _trace_hit(hit: SearchHit, *, status: str | None = None, provider: str = "") -> dict[str, Any]:
    """Bounded hit metadata; snippets remain explicitly untrusted."""

    metadata: dict[str, Any] = {
        "title": _trace_text(hit.title, _TRACE_TITLE_CHARS),
        "url": _trace_url(hit.url),
        "engine": _trace_text(hit.source_engine or provider, _TRACE_ENGINE_CHARS),
        "status": status if status is not None else hit.status,
    }
    snippet = _trace_snippet(hit.snippet)
    if snippet is not None:
        metadata["snippet"] = snippet
    return metadata


def _trace_diagnostic(value: object) -> object:
    """Keep provider diagnostics structured, small, and string-bounded."""

    if isinstance(value, dict):
        return {
            _trace_text(key, _TRACE_ENGINE_CHARS): _trace_diagnostic(item)
            for key, item in list(value.items())[:_TRACE_MAX_DIAGNOSTIC_ITEMS]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_trace_diagnostic(item) for item in list(value)[:_TRACE_MAX_DIAGNOSTIC_ITEMS]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _trace_text(value, _TRACE_ENGINE_CHARS) if isinstance(value, str) else value
    return _trace_text(value, _TRACE_ENGINE_CHARS)


@runtime_checkable
class RetrievalEngine(Protocol):
    """[CONTRACT] The orchestrator of the pipeline."""

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult: ...


class VectorStoreLike(Protocol):
    async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]: ...


@runtime_checkable
class HitExtractionProvider(Protocol):
    """Optional affinity-preserving extraction extension."""

    async def extract_hits(self, hits: list[SearchHit]) -> list[ExtractedDoc]: ...


async def extract_discovered_hits(
    extraction: ExtractionProvider, hits: list[SearchHit]
) -> list[ExtractedDoc]:
    """Extract discovery hits without discarding optional provider affinity.

    The stable extraction protocol remains URL-only. Composite providers may
    additionally implement ``extract_hits`` when discovery provenance determines
    which extractor can read a result (notably MCP search/fetch pairs).
    """
    if isinstance(extraction, HitExtractionProvider):
        docs = await extraction.extract_hits(hits)
    else:
        docs = await extraction.extract_many([hit.url for hit in hits])
    return [reject_challenge_page(doc) for doc in docs]


def annotate_passage_dates(
    extracted: list[ExtractedDoc], hits: list[SearchHit]
) -> list[ExtractedDoc]:
    """Carry exact discovery dates onto citable extracted passages by source id."""

    dates = {
        source_url_key(hit.url): hit.published_at for hit in hits if hit.published_at is not None
    }
    if not dates:
        return extracted
    annotated: list[ExtractedDoc] = []
    for document in extracted:
        published_at = dates.get(source_url_key(document.url))
        if published_at is None:
            annotated.append(document)
            continue
        passages = [
            passage
            if passage.published_at is not None
            else passage.model_copy(update={"published_at": published_at})
            for passage in document.passages
        ]
        annotated.append(document.model_copy(update={"passages": passages}))
    return annotated


def _build_retrieval_trace(
    req: RetrievalRequest,
    raw_hits: list[SearchHit],
    candidate_hits: list[SearchHit],
    extracted: list[ExtractedDoc],
    ranked: list[Passage],
    status_by_url: dict[str, str],
    provider_name: str,
    provider_diagnostic: object,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Assemble the bounded retrieval trace without changing pipeline order."""
    extraction_statuses = [
        {
            "url": _trace_url(doc.url),
            "title": _trace_text(doc.title, _TRACE_TITLE_CHARS),
            "status": doc.status,
            "fetched_ok": bool(doc.fetched_ok),
        }
        for doc in extracted[:_TRACE_MAX_HITS_PER_QUERY]
    ]
    for doc, status in zip(extracted[:_TRACE_MAX_HITS_PER_QUERY], extraction_statuses, strict=True):
        error_class = _extraction_error_class(doc)
        if error_class is not None:
            status["error_class"] = error_class
    issued_query = _trace_text(req.query, _TRACE_QUERY_CHARS)
    query_trace = {
        "planned_query": issued_query,
        "issued_query": issued_query,
        "raw_discovered_hit_count": len(raw_hits),
        "hits": [
            _trace_hit(
                hit,
                status=status_by_url.get(source_url_key(hit.url)),
                provider=provider_name,
            )
            for hit in raw_hits[:_TRACE_MAX_HITS_PER_QUERY]
        ],
        "extraction": {
            "attempted": sum(
                status_by_url.get(source_url_key(hit.url)) is not None for hit in raw_hits
            ),
            "success": sum(status_by_url.get(source_url_key(hit.url)) == "ok" for hit in raw_hits),
            "failure": sum(
                status_by_url.get(source_url_key(hit.url)) is not None
                and status_by_url.get(source_url_key(hit.url)) != "ok"
                for hit in raw_hits
            ),
        },
        **(
            {"provider_diagnostic": _trace_diagnostic(provider_diagnostic)}
            if provider_diagnostic
            else {}
        ),
    }
    retrieval_trace = {
        "planned_query": issued_query,
        "issued_queries": [issued_query],
        "provider": provider_name,
        "provider_diagnostics": (
            [_trace_diagnostic(provider_diagnostic)] if provider_diagnostic else []
        ),
        "raw_discovered_hit_count": len(raw_hits),
        "queries": [query_trace],
        "extraction": {
            "attempted": len(candidate_hits),
            "success": sum(1 for doc in extracted if doc.fetched_ok),
            "failure": sum(1 for doc in extracted if not doc.fetched_ok),
            "statuses": extraction_statuses,
        },
        "reranked_passage_count": len(ranked),
    }
    return issued_query, extraction_statuses, retrieval_trace


class DefaultRetrievalEngine:
    """[CONTRACT behavior; INTERIOR code] The reference pipeline."""

    def __init__(
        self,
        search: SearchProvider,
        extraction: ExtractionProvider,
        reranker: Reranker,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStoreLike | None = None,
        candidate_cap: int = _CANDIDATE_CAP,
    ) -> None:
        self._search = search
        self._extraction = extraction
        self._reranker = reranker
        self._embedder = embedder
        self._vector_store = vector_store
        self._candidate_cap = candidate_cap

    async def _search_one(self, req: RetrievalRequest) -> tuple[list[SearchHit], object]:
        """Issue `req.query`, retrying BELOW the model while the provider is degraded.

        A zero-hit response that names unresponsive engines (or carries a
        transport error) is a provider outage, not a query miss — re-issuing the
        same query is system mechanics, so it happens here and never reaches the
        caller as a research decision. The attempt record rides along in the
        diagnostic so an exhausted outage stays classifiable downstream.

        Both provider shapes are paced by the same process-wide bucket, so the
        searches of every concurrent run leave this host at one bounded rate.
        """
        limit = req.discover_limit or max(req.top_k * 3, 10)
        detailed = getattr(self._search, "search_detailed", None)
        if callable(detailed):

            async def attempt() -> tuple[list[SearchHit], dict[str, object]]:
                return await cast(Any, detailed)(
                    req.query,
                    limit=limit,
                    domains_allow=req.domains_allow,
                    domains_deny=req.domains_deny,
                    time_filter=req.recency_window,
                )

            hits, diagnostic = await search_with_degradation_retry(attempt)
            return hits, _trace_diagnostic(diagnostic)
        waited_ms = int(await acquire_search_slot() * 1_000)
        hits = await self._search.search(
            req.query,
            limit=limit,
            domains_allow=req.domains_allow,
            domains_deny=req.domains_deny,
            time_filter=req.recency_window,
        )
        return hits, {RATE_WAIT_KEY: waited_ms} if waited_ms else {}

    async def _discover(self, req: RetrievalRequest) -> tuple[list[SearchHit], object]:
        """Stage 2: one search of the query as written, in provider rank order.

        Hits are deduplicated by canonical URL: providers return the same page
        twice under different tracking parameters, and extraction must not fetch
        it twice or cite it as two sources.

        A corpus-only request (``use_web`` off) has no provider response to
        show, so it yields no hits and an empty diagnostic rather than a
        fabricated one.
        """
        if not req.use_web:
            return [], {}
        direct = _direct_discovery(req)
        if direct is not None:
            return direct
        hits, diagnostic = await self._search_one(req)
        return deduplicate_search_hits(hits), diagnostic

    async def _corpus_passages(self, req: RetrievalRequest) -> list[Passage]:
        """Space-corpus retrieval, scoped to the requested namespaces (§4)."""
        if not req.corpus_ids or self._vector_store is None or self._embedder is None:
            return []
        qvec = (await self._embedder.embed([req.query]))[0]
        out: list[Passage] = []
        for namespace in req.corpus_ids:
            out.extend(await self._vector_store.query(namespace, qvec, top_k=req.top_k))
        return out

    async def _extract_candidates(self, hits: list[SearchHit]) -> list[ExtractedDoc]:
        if not hits:
            return []
        extracted = await extract_discovered_hits(self._extraction, hits)
        return annotate_passage_dates(extracted, hits)

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        # One query in, one search out. `issued_queries` and the trace's
        # `queries` list stay lists: the wire shape the UI and the harness read
        # is unchanged, it now always holds exactly one entry.
        raw_hits, provider_diagnostic = await self._discover(req)
        candidate_cap = min(self._candidate_cap, req.extract_cap or self._candidate_cap)
        candidate_hits = [] if req.discovery_only else raw_hits[:candidate_cap]
        extracted = await self._extract_candidates(candidate_hits)

        # Discovery and extraction are different facts.  Carry extraction
        # truth back onto every attempted hit; leave unattempted hits as None.
        status_by_url = {source_url_key(doc.url): doc.status for doc in extracted}
        all_hits = [
            hit.model_copy(update={"status": status_by_url.get(source_url_key(hit.url))})
            for hit in raw_hits
        ]

        # Passages come from EXTRACTED content + corpora — never from snippets.
        candidates: list[Passage] = [p for doc in extracted if doc.fetched_ok for p in doc.passages]
        candidates += await self._corpus_passages(req)

        ranked = await self._reranker.rerank(
            req.query, candidates, top_k=len(candidates) if req.distinct_sources else req.top_k
        )
        if req.distinct_sources:
            ranked = distinct_source_passages(ranked, top_k=req.top_k)
        provider_name = _trace_text(getattr(self._search, "name", ""), _TRACE_ENGINE_CHARS)
        issued_query, extraction_statuses, retrieval_trace = _build_retrieval_trace(
            req,
            raw_hits,
            candidate_hits,
            extracted,
            ranked,
            status_by_url,
            provider_name,
            provider_diagnostic,
        )
        direct = req.use_web and _source_url(req.query) is not None
        if direct:
            _direct_trace(retrieval_trace)
        notes = {
            "candidates": len(candidates),
            "depth": req.depth,
            "discovered": 0 if direct else len(all_hits),
            "extracted": len(extracted),
            "retrieval_trace": retrieval_trace,
        }
        if req.discovery_only:
            notes["discovery_only"] = True
        return RetrievalResult(
            passages=ranked,
            all_hits=all_hits,  # full discovery set incl. failed/blocked (§2.2)
            extracted=extracted,
            issued_queries=[] if direct else [req.query],
            notes=notes,
        )

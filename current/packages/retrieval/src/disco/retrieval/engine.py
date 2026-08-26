"""The retrieval engine — retrieval-grounding-contract.md §3.

Orchestrates the quality pipeline: query transformation (by `depth`) → broad
retrieval (RRF-fused for multi-query) → extraction with provenance → rerank →
return. Passages come ONLY from extracted docs / corpora — never from a
SearchHit snippet (principle 4 "never trust the snippet").
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._query_compression import compress_search_query
from .models import ExtractedDoc, Passage, RetrievalRequest, RetrievalResult, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, QueryRewriter, Reranker, reciprocal_rank_fusion
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
_TRACE_ERROR_CLASS_CHARS = 32
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


class ProviderOperationError(RuntimeError):
    """A configured detailed provider failed every retrieval operation."""

    def __init__(self, diagnostic: object) -> None:
        self.diagnostic = _trace_diagnostic(diagnostic)
        super().__init__("configured retrieval provider failed all retrieval operations")


_NON_FAILURE_OUTCOMES = frozenset({"ok", "empty"})


def _is_explicit_provider_failure(diagnostic: object) -> bool:
    """Recognize only explicit provider or aggregate failure facts.

    Legacy providers intentionally have no outcome field; their empty result
    is healthy/unknown rather than evidence of an outage.  Detailed providers
    expose a provider name and outcome, so a failed adapter cannot be mistaken
    for a valid empty result. Multi-provider adapters expose the aggregate
    beside the provider map.
    """

    if not isinstance(diagnostic, dict):
        return False
    if diagnostic.get("provider_aggregate") == "all_failed":
        return True
    provider = diagnostic.get("provider")
    outcome = diagnostic.get("outcome")
    return bool(provider) and isinstance(outcome, str) and outcome not in _NON_FAILURE_OUTCOMES


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


def _extraction_error_class(doc: ExtractedDoc) -> str | None:
    """Classify an extraction failure without retaining provider error text."""

    status = _trace_text(doc.status, _TRACE_ERROR_CLASS_CHARS).casefold()
    error = _trace_text(doc.error, 240).casefold()
    if doc.fetched_ok and status == "ok":
        return None
    return (
        _status_error_class(status)
        or _text_error_class(error)
        or _http_error_class(error)
        or "other"
    )


def _status_error_class(status: str) -> str | None:
    if status == "blocked":
        return "anti_bot"
    if status == "paywalled":
        return "paywalled"
    if status == "not_found":
        return "not_found"
    return None


def _text_error_class(error: str) -> str | None:
    if any(
        marker in error
        for marker in (
            "captcha",
            "cloudflare",
            "anti-bot",
            "antibot",
            "robot check",
            "access denied",
            "forbidden",
        )
    ):
        return "anti_bot"
    if any(marker in error for marker in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return "timeout"
    if any(
        marker in error for marker in ("empty content", "no readable content", "no result returned")
    ):
        return "empty_content"
    return None


def _http_error_class(error: str) -> str | None:
    """Classify numeric response codes only in an HTTP-like context."""
    for match in re.finditer(r"\b([3-5][0-9]{2})\b", error):
        context = error[max(0, match.start() - 20) : min(len(error), match.end() + 20)]
        if any(marker in context for marker in ("http", "status", "response", "error", "redirect")):
            code = match.group(1)
            return f"redirect/http_{code}" if code.startswith("3") else f"upstream_http_{code}"
    return None


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
    docs, _diagnostic = await extract_discovered_hits_detailed(extraction, hits)
    return docs


async def extract_discovered_hits_detailed(
    extraction: ExtractionProvider, hits: list[SearchHit]
) -> tuple[list[ExtractedDoc], object]:
    """Extract discovered hits and retain an optional bounded provider fact."""

    if isinstance(extraction, HitExtractionProvider):
        return await extraction.extract_hits(hits), {}
    detailed = getattr(extraction, "extract_many_detailed", None)
    if callable(detailed):
        return await cast(Any, detailed)([hit.url for hit in hits])
    return await extraction.extract_many([hit.url for hit in hits]), {}


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


async def _extract_for_retrieval(
    extraction: ExtractionProvider, candidate_hits: list[SearchHit]
) -> tuple[list[ExtractedDoc], object]:
    if not candidate_hits:
        return [], {}
    extracted, diagnostic = await extract_discovered_hits_detailed(extraction, candidate_hits)
    if (
        not extracted or all(not doc.fetched_ok for doc in extracted)
    ) and _is_explicit_provider_failure(diagnostic):
        raise ProviderOperationError({"extraction": diagnostic})
    return annotate_passage_dates(extracted, candidate_hits), diagnostic


def _extraction_statuses(extracted: list[ExtractedDoc]) -> list[dict[str, Any]]:
    statuses = [
        {
            "url": _trace_url(doc.url),
            "title": _trace_text(doc.title, _TRACE_TITLE_CHARS),
            "status": doc.status,
            "fetched_ok": bool(doc.fetched_ok),
        }
        for doc in extracted[:_TRACE_MAX_HITS_PER_QUERY]
    ]
    for doc, status in zip(extracted[:_TRACE_MAX_HITS_PER_QUERY], statuses, strict=True):
        error_class = _extraction_error_class(doc)
        if error_class is not None:
            status["error_class"] = error_class
    return statuses


def _query_traces(
    req: RetrievalRequest,
    queries: list[str],
    raw_hit_lists: list[list[SearchHit]],
    provider_diagnostics: list[object],
    status_by_url: Mapping[str, str | None],
    provider_name: str,
) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for query, raw_hits, provider_diagnostic in zip(
        queries, raw_hit_lists, provider_diagnostics, strict=True
    ):
        traces.append(
            {
                "planned_query": _trace_text(req.query, _TRACE_QUERY_CHARS),
                "issued_query": _trace_text(query, _TRACE_QUERY_CHARS),
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
                    "success": sum(
                        status_by_url.get(source_url_key(hit.url)) == "ok" for hit in raw_hits
                    ),
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
        )
    return traces


def _retrieval_notes(
    req: RetrievalRequest,
    queries: list[str],
    all_hits: list[SearchHit],
    raw_hit_lists: list[list[SearchHit]],
    provider_diagnostics: list[object],
    extracted: list[ExtractedDoc],
    candidate_hits: list[SearchHit],
    candidates: list[Passage],
    ranked: list[Passage],
    provider_name: str,
    extraction_diagnostic: object,
) -> dict[str, Any]:
    status_by_url = {source_url_key(doc.url): doc.status for doc in extracted}
    return {
        "candidates": len(candidates),
        "depth": req.depth,
        "discovered": len(all_hits),
        "extracted": len(extracted),
        "retrieval_trace": {
            "planned_query": _trace_text(req.query, _TRACE_QUERY_CHARS),
            "issued_queries": [_trace_text(query, _TRACE_QUERY_CHARS) for query in queries],
            "provider": provider_name,
            "provider_diagnostics": [
                _trace_diagnostic(item) for item in provider_diagnostics if item
            ],
            "raw_discovered_hit_count": sum(len(hits) for hits in raw_hit_lists),
            "queries": _query_traces(
                req,
                queries,
                raw_hit_lists,
                provider_diagnostics,
                status_by_url,
                provider_name,
            ),
            "extraction": {
                "attempted": len(candidate_hits),
                "success": sum(1 for doc in extracted if doc.fetched_ok),
                "failure": sum(1 for doc in extracted if not doc.fetched_ok),
                "statuses": _extraction_statuses(extracted),
                **(
                    {"provider_diagnostic": _trace_diagnostic(extraction_diagnostic)}
                    if extraction_diagnostic
                    else {}
                ),
            },
            "reranked_passage_count": len(ranked),
        },
    }


class DefaultRetrievalEngine:
    """[CONTRACT behavior; INTERIOR code] The reference pipeline."""

    def __init__(
        self,
        search: SearchProvider,
        extraction: ExtractionProvider,
        reranker: Reranker,
        *,
        embedder: Embedder | None = None,
        rewriter: QueryRewriter | None = None,
        vector_store: VectorStoreLike | None = None,
        candidate_cap: int = _CANDIDATE_CAP,
    ) -> None:
        self._search = search
        self._extraction = extraction
        self._reranker = reranker
        self._embedder = embedder
        self._rewriter = rewriter
        self._vector_store = vector_store
        self._candidate_cap = candidate_cap

    async def _transform(self, req: RetrievalRequest) -> list[str]:
        """Stage 1: query transformation by depth (§3 step 1).

        Every query leaving this stage is compressed into keyword form
        (``_query_compression``) so ALL search providers — keyed and keyless —
        see engine-friendly queries instead of full interrogative sentences.
        ``req.query`` itself stays uncompressed: reranking, corpus embedding
        (`_corpus_passages`), and everything downstream of retrieval
        (gap reasoning, synthesis, judging) still see the full sub-question.
        """
        if req.depth == "shallow" or self._rewriter is None:
            return [compress_search_query(req.query)]
        try:
            if req.depth == "standard":
                rewritten = await self._rewriter.rewrite(req.query, n=1)
            else:
                rewritten = await self._rewriter.rewrite(req.query, n=4)  # deep → multi-query
        except Exception:  # noqa: BLE001 — rewriting is an optional search enhancement
            # A rejected or unavailable rewrite must not erase the search leg.
            # Search the user's query directly through the same retrieval path;
            # later adaptive probes can still broaden or deepen it normally.
            rewritten = [req.query]
        return [compress_search_query(q) for q in rewritten]

    async def _discover(
        self, req: RetrievalRequest, queries: list[str]
    ) -> tuple[list[SearchHit], list[list[SearchHit]], list[object]]:
        """Stage 2: broad retrieval across queries, RRF-fused for multi-query.

        Keep the individual provider responses long enough to make the
        per-query diagnostics truthful; the public result still exposes the
        same fused ``all_hits`` collection.
        """
        if not req.use_web:
            # Keep one diagnostic slot per transformed query even when web
            # discovery is intentionally disabled; the trace zipper below
            # remains shape-stable for corpus-only retrieval.
            return [], [[] for _ in queries], [{} for _ in queries]
        hit_lists: list[list[SearchHit]] = []
        diagnostics: list[object] = []
        all_provider_failures = 0
        for q in queries:
            detailed = getattr(self._search, "search_detailed", None)
            if callable(detailed):
                hits, diagnostic = await cast(Any, detailed)(
                    q,
                    limit=req.discover_limit or max(req.top_k * 3, 10),
                    domains_allow=req.domains_allow,
                    domains_deny=req.domains_deny,
                    time_filter=req.recency_window,
                )
            else:
                hits = await self._search.search(
                    q,
                    limit=req.discover_limit or max(req.top_k * 3, 10),
                    domains_allow=req.domains_allow,
                    domains_deny=req.domains_deny,
                    time_filter=req.recency_window,
                )
                diagnostic = {}
            if _is_explicit_provider_failure(diagnostic):
                all_provider_failures += 1
            hit_lists.append(hits)
            diagnostics.append(_trace_diagnostic(diagnostic))
        if queries and all_provider_failures == len(queries):
            raise ProviderOperationError({"queries": diagnostics})
        return reciprocal_rank_fusion(hit_lists), hit_lists, diagnostics

    async def _corpus_passages(self, req: RetrievalRequest) -> list[Passage]:
        """Space-corpus retrieval, scoped to the requested namespaces (§4)."""
        if not req.corpus_ids or self._vector_store is None or self._embedder is None:
            return []
        qvec = (await self._embedder.embed([req.query]))[0]
        out: list[Passage] = []
        for namespace in req.corpus_ids:
            out.extend(await self._vector_store.query(namespace, qvec, top_k=req.top_k))
        return out

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        queries = await self._transform(req)

        all_hits, raw_hit_lists, provider_diagnostics = await self._discover(req, queries)
        candidate_cap = min(self._candidate_cap, req.extract_cap or self._candidate_cap)
        candidate_hits = all_hits[:candidate_cap]
        extracted, extraction_diagnostic = await _extract_for_retrieval(
            self._extraction, candidate_hits
        )

        # Discovery and extraction are different facts.  Carry extraction
        # truth back onto every attempted hit; leave unattempted hits as None.
        status_by_url = {source_url_key(doc.url): doc.status for doc in extracted}
        all_hits = [
            hit.model_copy(update={"status": status_by_url.get(source_url_key(hit.url))})
            for hit in all_hits
        ]

        # Passages come from EXTRACTED content + corpora — never from snippets.
        candidates: list[Passage] = [p for doc in extracted if doc.fetched_ok for p in doc.passages]
        candidates += await self._corpus_passages(req)

        ranked = await self._reranker.rerank(req.query, candidates, top_k=req.top_k)
        provider_name = _trace_text(getattr(self._search, "name", ""), _TRACE_ENGINE_CHARS)
        notes = _retrieval_notes(
            req,
            queries,
            all_hits,
            raw_hit_lists,
            provider_diagnostics,
            extracted,
            candidate_hits,
            candidates,
            ranked,
            provider_name,
            extraction_diagnostic,
        )
        return RetrievalResult(
            passages=ranked,
            all_hits=all_hits,  # full discovery set incl. failed/blocked (§2.2)
            extracted=extracted,
            issued_queries=queries,
            notes=notes,
        )

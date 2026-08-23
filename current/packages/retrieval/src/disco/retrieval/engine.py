"""The retrieval engine — retrieval-grounding-contract.md §3.

Orchestrates the quality pipeline: query transformation (by `depth`) → broad
retrieval (RRF-fused for multi-query) → extraction with provenance → rerank →
return. Passages come ONLY from extracted docs / corpora — never from a
SearchHit snippet (principle 4 "never trust the snippet").
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._query_compression import compress_search_query
from .models import ExtractedDoc, Passage, RetrievalRequest, RetrievalResult, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, QueryRewriter, Reranker, reciprocal_rank_fusion
from .url_policy import source_url_key

# Default cap on URLs sent to extraction per retrieve() (cost bound; [INTERIOR]).
_CANDIDATE_CAP = 20


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
        return await extraction.extract_hits(hits)
    return await extraction.extract_many([hit.url for hit in hits])


def annotate_passage_dates(
    extracted: list[ExtractedDoc], hits: list[SearchHit]
) -> list[ExtractedDoc]:
    """Carry exact discovery dates onto citable extracted passages by source id."""

    dates = {
        source_url_key(hit.url): hit.published_at
        for hit in hits
        if hit.published_at is not None
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

    async def _discover(self, req: RetrievalRequest, queries: list[str]) -> list[SearchHit]:
        """Stage 2: broad retrieval across queries, RRF-fused for multi-query."""
        if not req.use_web:
            return []
        hit_lists: list[list[SearchHit]] = []
        for q in queries:
            hit_lists.append(
                await self._search.search(
                    q,
                    limit=req.discover_limit or max(req.top_k * 3, 10),
                    domains_allow=req.domains_allow,
                    domains_deny=req.domains_deny,
                    time_filter=req.recency_window,
                )
            )
        if len(hit_lists) > 1:
            return reciprocal_rank_fusion(hit_lists)
        return hit_lists[0] if hit_lists else []

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

        all_hits = await self._discover(req, queries)
        candidate_cap = min(self._candidate_cap, req.extract_cap or self._candidate_cap)
        candidate_hits = all_hits[:candidate_cap]
        if candidate_hits:
            extracted = await extract_discovered_hits(self._extraction, candidate_hits)
            extracted = annotate_passage_dates(extracted, candidate_hits)
        else:
            extracted = []

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
        return RetrievalResult(
            passages=ranked,
            all_hits=all_hits,  # full discovery set incl. failed/blocked (§2.2)
            extracted=extracted,
            issued_queries=queries,
            notes={
                "candidates": len(candidates),
                "depth": req.depth,
                "discovered": len(all_hits),
                "extracted": len(extracted),
            },
        )

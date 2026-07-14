"""The retrieval engine — retrieval-grounding-contract.md §3.

Orchestrates the quality pipeline: query transformation (by `depth`) → broad
retrieval (RRF-fused for multi-query) → extraction with provenance → rerank →
return. Passages come ONLY from extracted docs / corpora — never from a
SearchHit snippet (principle 4 "never trust the snippet").
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ExtractedDoc, Passage, RetrievalRequest, RetrievalResult, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, QueryRewriter, Reranker, reciprocal_rank_fusion

# Default cap on URLs sent to extraction per retrieve() (cost bound; [INTERIOR]).
_CANDIDATE_CAP = 20


@runtime_checkable
class RetrievalEngine(Protocol):
    """[CONTRACT] The orchestrator of the pipeline."""

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult: ...


class VectorStoreLike(Protocol):
    async def query(self, namespace: str, vector: list[float], *, top_k: int) -> list[Passage]: ...


async def extract_discovered_hits(
    extraction: ExtractionProvider, hits: list[SearchHit]
) -> list[ExtractedDoc]:
    """Extract discovery hits without discarding optional provider affinity.

    The stable extraction protocol remains URL-only. Composite providers may
    additionally implement ``extract_hits`` when discovery provenance determines
    which extractor can read a result (notably MCP search/fetch pairs).
    """
    extract_hits = getattr(extraction, "extract_hits", None)
    if callable(extract_hits):
        return await extract_hits(hits)
    return await extraction.extract_many([hit.url for hit in hits])


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
        """Stage 1: query transformation by depth (§3 step 1)."""
        if req.depth == "shallow" or self._rewriter is None:
            return [req.query]
        if req.depth == "standard":
            return await self._rewriter.rewrite(req.query, n=1)
        return await self._rewriter.rewrite(req.query, n=4)  # deep → multi-query

    async def _discover(self, req: RetrievalRequest, queries: list[str]) -> list[SearchHit]:
        """Stage 2: broad retrieval across queries, RRF-fused for multi-query."""
        if not req.use_web:
            return []
        hit_lists: list[list[SearchHit]] = []
        for q in queries:
            hit_lists.append(
                await self._search.search(
                    q,
                    limit=max(req.top_k * 3, 10),
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
        candidate_hits = all_hits[: self._candidate_cap]
        if candidate_hits:
            extracted = await extract_discovered_hits(self._extraction, candidate_hits)
        else:
            extracted = []

        # Passages come from EXTRACTED content + corpora — never from snippets.
        candidates: list[Passage] = [p for doc in extracted if doc.fetched_ok for p in doc.passages]
        candidates += await self._corpus_passages(req)

        ranked = await self._reranker.rerank(req.query, candidates, top_k=req.top_k)
        return RetrievalResult(
            passages=ranked,
            all_hits=all_hits,  # full discovery set incl. failed/blocked (§2.2)
            extracted=extracted,
            issued_queries=queries,
            notes={"candidates": len(candidates), "depth": req.depth},
        )

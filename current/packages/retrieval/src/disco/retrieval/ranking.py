"""Reranking, embedding, query transformation — retrieval-grounding-contract.md §3.

Reranking is the #1 quality lever (principle 1). The protocols are contractual;
the shipped implementations are deterministic stubs standing in for the real
checkpoints ([VERIFY] `bge-reranker-v2-m3`, `bge-m3`) so the pipeline logic is
testable headless. `reciprocal_rank_fusion` fuses multi-query result lists (§3.2).
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol, runtime_checkable

from disco.core import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole
from disco.core.think import strip_think_spans

from .models import Passage, SearchHit
from .url_policy import source_url_key

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


# ---- protocols (§3.1) -------------------------------------------------------


@runtime_checkable
class Reranker(Protocol):
    """[CONTRACT] Scores (query, passage) pairs; keeps the top_k."""

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]: ...


@runtime_checkable
class Embedder(Protocol):
    """[CONTRACT] Vectors for passages/queries (vector store + hybrid)."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class QueryRewriter(Protocol):
    """[CONTRACT] Produces query paraphrases (one for `standard`, several for
    `deep`) via the QUERY_REWRITER role (router §15.2)."""

    async def rewrite(self, query: str, *, n: int) -> list[str]: ...


# ---- RRF (§3 step 2) --------------------------------------------------------


def reciprocal_rank_fusion(hit_lists: list[list[SearchHit]], *, k: int = 60) -> list[SearchHit]:
    """[CONTRACT semantics] Fuse multiple ranked SearchHit lists by URL: a hit's
    score is sum over lists of 1/(k + rank). A URL ranked across multiple
    queries outranks one ranked high in only a single query (§8.2)."""
    scores: dict[str, float] = {}
    first_seen: dict[str, SearchHit] = {}
    for hits in hit_lists:
        for rank, hit in enumerate(hits):
            key = source_url_key(hit.url)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(key, hit)
    ordered = sorted(first_seen.items(), key=lambda item: scores[item[0]], reverse=True)
    return [hit for _key, hit in ordered]


# ---- shipped stub implementations -------------------------------------------


class LexicalReranker:
    """[INTERIOR stub] Scores passages by query-term overlap. Deterministic;
    stands in for the self-hosted cross-encoder (`bge-reranker-v2-m3`) until the
    real checkpoint is wired. Stable sort preserves input order on ties."""

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
        q = _tokens(query)
        scored = sorted(
            enumerate(passages),
            key=lambda ip: (-len(q & _tokens(ip[1].text)), ip[0]),
        )
        return [p for _, p in scored[:top_k]]


class HashingEmbedder:
    """[INTERIOR stub] Deterministic bag-of-words hashing embedder (stands in for
    `bge-m3`). Good enough to exercise the vector store; not a quality signal."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _tokens(text):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim  # noqa: S324
                vec[h] += 1.0
            out.append(vec)
        return out


class RouterQueryRewriter:
    """[CONTRACT] Query rewriter backed by the router's QUERY_REWRITER role.
    Asks for `n` paraphrases and parses them line by line."""

    def __init__(self, router: LLMRouter) -> None:
        self._router = router

    async def rewrite(self, query: str, *, n: int) -> list[str]:
        if n <= 1:
            instruction = (
                f"Rewrite this search query to be clearer; output only the query:\n{query}"
            )
        else:
            instruction = (
                f"Generate {n} diverse paraphrases of this search query, one per line:\n{query}"
            )
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.QUERY_REWRITER),
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "Rewrite search queries without changing their subject, named "
                        "entities, time window, exclusions, or requested scope. Output "
                        "only the requested query lines."
                    ),
                ),
                LLMMessage(role="user", content=instruction),
            ],
            temperature=0.0,
        )
        resp = await self._router.complete(req)
        # A leaked <think> preamble here is catastrophic: its first line becomes
        # the literal engine query (live-caught: DDG returned "rewrite a title"
        # SEO junk for a telegraph-history question). Strip before parsing.
        text = strip_think_spans(resp.text)
        lines = [ln.strip(" -•\t") for ln in text.splitlines() if ln.strip()]
        return lines[:n] or [query]

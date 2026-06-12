"""In-process encoders — the non-generative models bundled INTO the app so a
grounded answer needs no separate encoder server (portability; faster than the
network hop). ONNX/CPU via `fastembed` — deliberately NOT torch (light, and no
ROCm pain on AMD).

Three roles, two models:
  - Embedder  → `multilingual-e5-large`  (dense vectors; Space-corpus retrieval)
  - Reranker  → `jina-reranker-v2-base-multilingual`  (cross-encoder relevance)
  - NLI       → the SAME cross-encoder, framed as claim↔passage entailment
                (replaces the lexical-overlap stub with a real signal)

These are equivalent-class peers of the deployed bge-m3 / bge-reranker-v2-m3 —
fastembed doesn't ship those exact ids, and the vector store is per-run (no
persisted vectors to migrate), so the swap is safe.

Models load LAZILY on first use (the first call downloads ~1GB + loads to RAM)
and are cached process-wide, so the cost is paid once. fastembed is synchronous
and CPU-bound, so async methods offload to a thread to keep the loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from .models import Passage
from .nli import Entailment

_log = logging.getLogger(__name__)

# Equivalent-class multilingual models fastembed ships as ready ONNX.
EMBED_MODEL = "intfloat/multilingual-e5-large"
RERANK_MODEL = "jinaai/jina-reranker-v2-base-multilingual"

# A cross-encoder's attention is O(seq_len^2) per (query, passage) pair, and
# fastembed does NOT truncate inputs to the model's window — so reranking
# full-page passages (thousands of tokens) blows ONNX attention memory up to
# ~16 GB and OOMs the box. Cap the SCORING input to ~512 tokens (the relevance
# signal lives in the head); the returned Passage objects keep their full text.
_RERANK_MAX_CHARS = 2000
# Also bound the BATCH: cross-encoder memory scales with batch×seq², so scoring
# all hits in one shot (unbounded passage count) still spikes. Score in chunks so
# peak memory is O(batch) regardless of how many passages came back.
_RERANK_BATCH = 8

# Process-wide single load (downloads once, stays resident).
_embedding_model: Any | None = None
_cross_encoder: Any | None = None


def _embedding() -> Any:
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding

        _embedding_model = TextEmbedding(model_name=EMBED_MODEL)
    return _embedding_model


def _reranker() -> Any:
    global _cross_encoder
    if _cross_encoder is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _cross_encoder = TextCrossEncoder(model_name=RERANK_MODEL)
    return _cross_encoder


def _sigmoid(x: float) -> float:
    # cross-encoder scores are unbounded logits; squash to a 0..1 probability.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


class FastEmbedEmbedder:
    """[Embedder] Dense embeddings via fastembed (multilingual-e5-large, 1024-d)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        def _run() -> list[list[float]]:
            return [vec.tolist() for vec in _embedding().embed(texts)]

        return await asyncio.to_thread(_run)


class FastEmbedReranker:
    """[Reranker] Cross-encoder rerank via fastembed (jina-reranker-v2-multilingual).
    Degrades to input order on any failure (never crashes the pipeline)."""

    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
        if not passages:
            return []

        def _run() -> list[float]:
            docs = [p.text[:_RERANK_MAX_CHARS] for p in passages]
            scores: list[float] = []
            for i in range(0, len(docs), _RERANK_BATCH):
                scores.extend(_reranker().rerank(query, docs[i : i + _RERANK_BATCH]))
            return scores

        try:
            scores = await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001 — degrade to input order, like the remote path
            # Degrade gracefully, but NEVER silently: a swallowed rerank failure
            # means the pipeline ships unranked results with no signal. Log it loud.
            _log.warning("rerank failed (%s: %s) — degrading to input order", type(e).__name__, e)
            return passages[:top_k]
        order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)
        return [passages[i] for i in order[:top_k]]


class FastEmbedNLIVerifier:
    """[NLIVerifier] Real entailment signal from the SAME cross-encoder, framed as
    claim↔passage relevance (a strong proxy for 'does this passage support the
    claim'), replacing the lexical-overlap stub. SYNC per the interface; results
    cached per (premise, claim). A true 3-class NLI model is a further refinement
    — this is a real signal, not a heuristic."""

    def __init__(self, *, entail_at: float = 0.5, contradict_below: float = 0.1) -> None:
        self._entail_at = entail_at
        self._contradict_below = contradict_below
        self._cache: dict[tuple[str, str], float] = {}

    def score(self, premise: str, hypothesis: str) -> float:
        key = (premise, hypothesis)
        if key not in self._cache:
            try:
                raw = list(_reranker().rerank(hypothesis, [premise]))[0]
                self._cache[key] = _sigmoid(float(raw))
            except Exception:  # noqa: BLE001 — failure → neutral, never crash
                self._cache[key] = 0.0
        return self._cache[key]

    def entail(self, premise: str, hypothesis: str) -> Entailment:
        s = self.score(premise, hypothesis)
        if s >= self._entail_at:
            return "entail"
        if s <= self._contradict_below:
            return "contradict"
        return "neutral"

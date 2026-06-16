"""In-process encoders — the non-generative models bundled INTO the app so a
grounded answer needs no separate encoder server (portability; faster than the
network hop). ONNX/CPU via `fastembed` — deliberately NOT torch (light, and no
ROCm pain on AMD).

Three roles, two models:
  - Embedder  → `multilingual-e5-large`  (dense vectors; Space-corpus retrieval)
  - Reranker  → `BAAI/bge-reranker-base`  (cross-encoder relevance, MIT)
  - NLI       → the SAME cross-encoder, framed as claim↔passage entailment
                (replaces the lexical-overlap stub with a real signal)

LICENSE NOTE: the default reranker is MIT (commercial-safe). It was previously
`jinaai/jina-reranker-v2-base-multilingual`, which is CC-BY-NC-4.0 (non-commercial)
and so shipped an unsafe default for any commercial deployment — swapped to MIT
bge-reranker-base. fastembed doesn't ship bge-reranker-v2-m3 (the broad-multilingual
MIT option) in this version; revisit on a fastembed upgrade if broad multilingual is
needed commercially. The vector store is per-run (no persisted vectors to migrate),
so the model swap is safe.

Models load LAZILY on first use (the first call downloads ~1GB + loads to RAM)
and are cached process-wide, so the cost is paid once. fastembed is synchronous
and CPU-bound, so async methods offload to a thread to keep the loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from disco.core.env import disco_env

from .models import Passage
from .nli import Entailment

_log = logging.getLogger(__name__)


class EncoderUnavailable(RuntimeError):
    """Raised when an in-process encoder cannot be loaded due to insufficient RAM.

    This is a typed signal the WebSocket handler can catch to emit an honest error
    frame instead of letting an OOM kill the process (exit 137 with a dead socket)."""


def _mem_available_gb() -> float:
    """Return available system RAM in GB by reading /proc/meminfo MemAvailable.

    Returns float('inf') on any read failure (non-Linux or /proc unavailable) so
    the guard becomes a no-op in those environments rather than a false block."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:  # noqa: BLE001
        pass
    return float("inf")  # non-Linux or read failure → skip the guard


# Minimum free RAM (GB) required before loading an encoder model (per-tier constant).
# full tier: two large ONNX models (~1 GB each) + runtime overhead → 4 GB guard.
# lite tier: two small ONNX models (~80 MB total) + runtime overhead → 0.5 GB guard.
_HEADROOM_GB_FULL = 4.0
_HEADROOM_GB_LITE = 0.5


def _require_ram(model_name: str) -> None:
    """Raise EncoderUnavailable if available system RAM is below the tier-appropriate
    headroom threshold.  Called BEFORE each lazy model instantiation so an OOM never
    silently kills the process — the caller gets a typed exception to handle."""
    tier = disco_env("ENCODER_TIER", _TIER_FULL).lower()
    headroom = _HEADROOM_GB_LITE if tier == _TIER_LITE else _HEADROOM_GB_FULL
    available = _mem_available_gb()
    if available < headroom:
        raise EncoderUnavailable(
            f"encoder load aborted: {available:.1f} GB RAM available, "
            f"need ~{headroom:.0f} GB for {model_name!r}. "
            f"Lower DISCO_ENCODER_TIER to 'lite' or free RAM before retrying."
        )


# Equivalent-class multilingual models fastembed ships as ready ONNX.
# Both are env-overridable so an operator can swap encoder backends (e.g. a
# self-hosted model) without a code change. The default ids preserve the
# original behaviour when the env is unset. (Read once at import — for
# per-call override in tests, set the env before importing; lazy loaders
# re-read the env at call time below so the tier-aware defaults still work.)
EMBED_MODEL = disco_env("EMBED_MODEL", "intfloat/multilingual-e5-large")
# Default reranker is MIT (commercial-safe). The broadly-multilingual
# jinaai/jina-reranker-v2-base-multilingual is CC-BY-NC-4.0 (NON-commercial) so it is
# NOT the default — set DISCO_RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual to
# opt back into it IF your use permits the NC license. bge-reranker-base is MIT,
# multilingual-capable (zh/en strong), and confirmed in fastembed's registry.
RERANK_MODEL = disco_env("RERANK_MODEL", "BAAI/bge-reranker-base")

# "lite" tier: small ONNX models for keyless / ≤8 GB boxes (~0.15 GB total).
# Both confirmed in fastembed's list_supported_models() on 2026-06-13.
#   BAAI/bge-small-en-v1.5   — 0.067 GB, 384-d, English, MIT
#   Xenova/ms-marco-MiniLM-L-6-v2 — 0.08 GB, Apache-2.0
EMBED_MODEL_LITE = "BAAI/bge-small-en-v1.5"
RERANK_MODEL_LITE = "Xenova/ms-marco-MiniLM-L-6-v2"

# DISCO_ENCODER_TIER: "lite" | "full" (default: "full")
# Defaults to "full" to preserve existing behaviour for any deployment that was
# already running the large models.  Set DISCO_ENCODER_TIER=lite explicitly for a
# keyless / ≤8 GB box.  An explicit DISCO_EMBED_MODEL or DISCO_RERANK_MODEL always
# wins over the tier default (B1a precedence preserved).
_TIER_LITE = "lite"
_TIER_FULL = "full"


def _tier_embed_default() -> str:
    """Return the tier-appropriate embed model id (no explicit override applied here)."""
    tier = disco_env("ENCODER_TIER", _TIER_FULL).lower()
    return EMBED_MODEL_LITE if tier == _TIER_LITE else EMBED_MODEL


def _tier_rerank_default() -> str:
    """Return the tier-appropriate rerank model id (no explicit override applied here)."""
    tier = disco_env("ENCODER_TIER", _TIER_FULL).lower()
    return RERANK_MODEL_LITE if tier == _TIER_LITE else RERANK_MODEL

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


def _arena_session_kwargs() -> dict[str, bool]:
    """ONNX Runtime session options for the encoders (fastembed 0.8 exposes
    `enable_cpu_mem_arena`).

    The CPU memory ARENA is DISABLED by default. It is the Deep Research RSS-floor
    culprit: the arena grows to the concurrent-inference peak (K gather legs each
    running bge-m3 + the cross-encoder) and NEVER shrinks, so a long-lived
    agent-server stays pinned at ~14 GB after an exhaustive run even though the
    memory is idle (live-confirmed: jemalloc could not reclaim it — it is held by
    the arena, not freed). With the arena off, each inference allocates and frees
    its working set, which the allocator then returns to the OS, so RSS recedes
    after a run. The cost is a small per-inference allocation overhead, a sound
    trade for a long-lived server (and essential on the 8 GB bundled tier, where a
    single quick DR otherwise retains ~7 GB). `DISCO_ENCODER_CPU_ARENA=on`
    re-enables the arena (marginally faster, much higher retained RSS)."""
    val = (disco_env("ENCODER_CPU_ARENA") or "off").strip().lower()
    return {"enable_cpu_mem_arena": val in ("on", "true", "1", "yes")}


def _embedding() -> Any:
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding

        # Explicit PMX_EMBED_MODEL wins; fall back to tier-aware default.
        model_name = disco_env("EMBED_MODEL") or _tier_embed_default()
        # RAM guard: raises EncoderUnavailable instead of letting an OOM kill the process.
        _require_ram(model_name)
        _embedding_model = TextEmbedding(model_name=model_name, **_arena_session_kwargs())
    return _embedding_model


def _reranker() -> Any:
    global _cross_encoder
    if _cross_encoder is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        # Explicit PMX_RERANK_MODEL wins; fall back to tier-aware default.
        model_name = disco_env("RERANK_MODEL") or _tier_rerank_default()
        # RAM guard: raises EncoderUnavailable instead of letting an OOM kill the process.
        _require_ram(model_name)
        _cross_encoder = TextCrossEncoder(model_name=model_name, **_arena_session_kwargs())
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
        except EncoderUnavailable:
            raise  # RAM guard: propagate so the WS handler emits an honest error frame
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
            except EncoderUnavailable:
                # RAM guard: propagate so the caller gets an honest error, not a wrong neutral
                raise
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

"""In-process encoders (fastembed) — wiring + a gated live-model check.

The wiring/conformance tests are pure (no model load). The functional tests
actually run the ONNX models, so each is skipped when the model IT needs isn't
loadable (no fastembed install / that model not cached / offline CI) — they run
locally where the models are cached.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable

import pytest
from disco.core.env import disco_env
from disco.core.llm.nli import NLIVerifier
from disco.retrieval.live import build_live_retrieval
from disco.retrieval.local_encoders import (
    FastEmbedEmbedder,
    FastEmbedNLIVerifier,
    FastEmbedReranker,
    _embedding,
    _embedding_factory,
    _reranker,
    _sigmoid,
    _tier_embed_default,
    _tier_rerank_default,
)
from disco.retrieval.models import Passage
from disco.retrieval.ranking import Embedder, Reranker

# ---- wiring (pure — no model load) ------------------------------------------


def test_encoders_are_local_by_default():
    d = build_live_retrieval(env={})
    assert type(d["reranker"]).__name__ == "FastEmbedReranker"
    assert type(d["embedder"]).__name__ == "FastEmbedEmbedder"
    assert type(d["nli"]).__name__ == "FastEmbedNLIVerifier"


def test_remote_encoders_are_opt_in():
    d = build_live_retrieval(env={"PMX_ENCODERS": "remote"})
    assert type(d["reranker"]).__name__ == "FastEmbedReranker"
    assert type(d["embedder"]).__name__ == "FastEmbedEmbedder"
    assert type(d["nli"]).__name__ == "FastEmbedNLIVerifier"

    d = build_live_retrieval(
        env={"PMX_ENCODERS": "remote"},
        origin_approved=lambda *_args: True,
    )
    assert type(d["reranker"]).__name__ == "TeiReranker"
    assert type(d["embedder"]).__name__ == "OpenAIEmbedder"
    assert type(d["nli"]).__name__ == "SidecarNLIVerifier"
    # search + extraction now default to the BUNDLED tiers (keyless composite /
    # local) — the universal first-run default — independent of the encoder mode.
    assert type(d["search"]).__name__ == "MultiSearchProvider"
    assert type(d["extraction"]).__name__ == "LocalExtractionProvider"


def test_providers_conform_to_their_protocols():
    assert isinstance(FastEmbedEmbedder(), Embedder)
    assert isinstance(FastEmbedReranker(), Reranker)
    assert isinstance(FastEmbedNLIVerifier(), NLIVerifier)


def test_sigmoid_squashes_logits_to_probabilities():
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert 0.0 < _sigmoid(-8) < _sigmoid(0) < _sigmoid(8) < 1.0


def test_multilingual_e5_pins_mean_pooling() -> None:
    factory = _embedding_factory("intfloat/multilingual-e5-large")

    assert factory.__name__ == "PooledEmbedding"


def test_nli_load_failure_is_visible_without_retrying_download_for_every_claim(monkeypatch):
    from disco.retrieval import local_encoders as le

    attempts = []

    def unavailable():
        attempts.append(True)
        raise OSError("offline")

    monkeypatch.setattr(le, "_nli_backend", unavailable)
    verifier = FastEmbedNLIVerifier()
    for claim in ("Claim one", "Claim two"):
        assert verifier.entail("Evidence", claim) == "neutral"
        assert verifier.score("Evidence", claim) == 0.0
        assert not verifier.verification_available("Evidence", claim)
    assert len(attempts) == 1
    assert verifier.verifier_failures == 2


def test_nli_label_score_and_availability_share_the_same_measurement(monkeypatch):
    from disco.retrieval import local_encoders as le
    from disco.retrieval.local_nli import NLIPrediction

    calls = []

    class Backend:
        def predict(self, premise, hypothesis):
            calls.append((premise, hypothesis))
            return NLIPrediction("contradict", 0.02)

    monkeypatch.setattr(le, "_nli_backend", Backend)
    verifier = FastEmbedNLIVerifier()
    assert verifier.entail("Evidence", "Claim") == "contradict"
    assert verifier.score("Evidence", "Claim") == 0.02
    assert verifier.verification_available("Evidence", "Claim")
    assert calls == [("Evidence", "Claim")]
    assert verifier.verifier_failures == 0


async def test_rerank_truncates_and_batches_to_bound_memory(monkeypatch):
    """Regression: a cross-encoder's attention is O(seq^2)*batch; scoring full-page
    passages all at once blew rerank to ~16GB and OOM-froze the box. The reranker
    must truncate each scoring input AND score in bounded batches."""
    from disco.retrieval import local_encoders as le

    calls: list[list[str]] = []

    class _FakeCE:
        def rerank(self, query, docs):
            docs = list(docs)
            calls.append(docs)
            return [float(len(d)) for d in docs]

    monkeypatch.setattr(le, "_reranker", lambda: _FakeCE())

    passages = [
        Passage(id=f"p{i}", source_url=f"http://e/{i}", source_title="t", text="x" * 10_000)
        for i in range(20)
    ]
    out = await le.FastEmbedReranker().rerank("q", passages, top_k=5)

    # Every doc handed to the cross-encoder was truncated to the cap...
    assert all(len(d) <= le._RERANK_MAX_CHARS for batch in calls for d in batch)
    # ...and scored in batches no larger than the bound (memory is O(batch), not O(N))...
    assert all(len(batch) <= le._RERANK_BATCH for batch in calls)
    # ...while still scoring all 20 passages and returning top_k.
    assert sum(len(b) for b in calls) == 20
    assert len(out) == 5


# ---- live model behavior (gated: needs the ONNX models loadable) ------------


def _fastembed_cache_dir() -> str:
    """Mirror of fastembed.common.utils.define_cache_dir's default resolution — read
    here rather than imported so the gate still works when fastembed isn't installed
    (one of the cases these tests are supposed to skip on, not error on)."""
    return os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
        tempfile.gettempdir(), "fastembed_cache"
    )


def _skip_reason(load: Callable[[], object], model_id: str) -> str:
    """Empty string if `load` succeeds; otherwise a skip reason naming the model.

    Guarding on ONE model made a half-warm cache FAIL instead of skip: with the
    reranker cached but the embedder missing (e.g. fastembed's default cache under
    the system temp dir wiped, HF_HUB_OFFLINE=1 blocking the refetch), the embedder
    test ran anyway and errored. Each live test now gates on the model IT loads, and
    the reason names that model and the cache dir to look in."""
    try:
        load()
    except Exception as exc:  # noqa: BLE001
        return (
            f"{model_id} not loadable from fastembed cache {_fastembed_cache_dir()} "
            f"({type(exc).__name__}: {exc})"
        )
    return ""


def _needs(load: Callable[[], object], model_id: str) -> pytest.MarkDecorator:
    reason = _skip_reason(load, model_id)
    return pytest.mark.skipif(bool(reason), reason=reason)


# Resolved the same way the loaders resolve them, so the reason names the model
# that was actually looked for (explicit override, else the tier default).
needs_reranker = _needs(_reranker, disco_env("RERANK_MODEL") or _tier_rerank_default())
needs_embedder = _needs(_embedding, disco_env("EMBED_MODEL") or _tier_embed_default())


@needs_reranker
async def test_reranker_orders_relevant_passage_first():
    rr = FastEmbedReranker()
    ps = [
        Passage(id="off", source_url="", source_title="", text="Bananas are rich in potassium."),
        Passage(id="hit", source_url="", source_title="", text="Paris is the capital of France."),
    ]
    out = await rr.rerank("What is the capital of France?", ps, top_k=2)
    assert out[0].id == "hit"


@needs_embedder
async def test_embedder_returns_dense_vectors_and_semantic_order():
    emb = FastEmbedEmbedder()
    v = await emb.embed(["a cat on a mat", "a feline on a rug", "stock market futures"])
    assert len(v) == 3 and len(v[0]) > 100  # dense vectors

    def cos(a, b):
        import math

        return sum(x * y for x, y in zip(a, b, strict=True)) / (
            math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        )

    assert cos(v[0], v[1]) > cos(v[0], v[2])  # related > unrelated


@pytest.mark.skipif(not disco_env("NLI_MODEL_DIR"), reason="NLI model not explicitly provisioned")
def test_nli_scores_a_supported_claim_above_an_unrelated_one():
    nli = FastEmbedNLIVerifier()
    supported = nli.score("Paris is the capital of France.", "The capital of France is Paris.")
    unrelated = nli.score("Bananas are rich in potassium.", "The capital of France is Paris.")
    assert supported > unrelated
    verdict = nli.entail("Paris is the capital of France.", "The capital of France is Paris.")
    assert verdict == "entail"

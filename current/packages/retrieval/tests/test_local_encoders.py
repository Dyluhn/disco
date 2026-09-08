"""In-process encoders (fastembed) — wiring + a gated live-model check.

The wiring/conformance tests are pure (no model load). The functional test
actually runs the ONNX models, so it's skipped when they aren't loadable (no
fastembed install / no cached model / offline CI) — it runs locally where the
models are cached.
"""

from __future__ import annotations

import pytest
from disco.core.env import disco_env
from disco.core.llm.nli import NLIVerifier
from disco.retrieval.live import build_live_retrieval
from disco.retrieval.local_encoders import (
    FastEmbedEmbedder,
    FastEmbedNLIVerifier,
    FastEmbedReranker,
    _embedding_factory,
    _sigmoid,
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


def _models_available() -> bool:
    try:
        from disco.retrieval.local_encoders import _reranker

        _reranker()  # loads from cache; raises if unavailable/offline
        return True
    except Exception:  # noqa: BLE001
        return False


live = pytest.mark.skipif(not _models_available(), reason="ONNX encoder models not loadable")


@live
async def test_reranker_orders_relevant_passage_first():
    rr = FastEmbedReranker()
    ps = [
        Passage(id="off", source_url="", source_title="", text="Bananas are rich in potassium."),
        Passage(id="hit", source_url="", source_title="", text="Paris is the capital of France."),
    ]
    out = await rr.rerank("What is the capital of France?", ps, top_k=2)
    assert out[0].id == "hit"


@live
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

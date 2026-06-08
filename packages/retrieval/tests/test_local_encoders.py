"""In-process encoders (fastembed) — wiring + a gated live-model check.

The wiring/conformance tests are pure (no model load). The functional test
actually runs the ONNX models, so it's skipped when they aren't loadable (no
fastembed install / no cached model / offline CI) — it runs locally where the
models are cached.
"""

from __future__ import annotations

import pytest
from perpleximanus.core.llm.nli import NLIVerifier
from perpleximanus.retrieval.live import build_live_retrieval
from perpleximanus.retrieval.local_encoders import (
    FastEmbedEmbedder,
    FastEmbedNLIVerifier,
    FastEmbedReranker,
    _sigmoid,
)
from perpleximanus.retrieval.models import Passage
from perpleximanus.retrieval.ranking import Embedder, Reranker

# ---- wiring (pure — no model load) ------------------------------------------


def test_encoders_are_local_by_default():
    d = build_live_retrieval(env={})
    assert type(d["reranker"]).__name__ == "FastEmbedReranker"
    assert type(d["embedder"]).__name__ == "FastEmbedEmbedder"
    assert type(d["nli"]).__name__ == "FastEmbedNLIVerifier"


def test_remote_encoders_are_opt_in():
    d = build_live_retrieval(env={"PMX_ENCODERS": "remote"})
    assert type(d["reranker"]).__name__ == "TeiReranker"
    assert type(d["embedder"]).__name__ == "OpenAIEmbedder"
    assert type(d["nli"]).__name__ == "SidecarNLIVerifier"
    # search + extraction now default to the BUNDLED tiers (ddgs/local) — keyless,
    # the universal first-run default — independent of the encoder mode.
    assert type(d["search"]).__name__ == "DdgsSearchProvider"
    assert type(d["extraction"]).__name__ == "LocalExtractionProvider"


def test_providers_conform_to_their_protocols():
    assert isinstance(FastEmbedEmbedder(), Embedder)
    assert isinstance(FastEmbedReranker(), Reranker)
    assert isinstance(FastEmbedNLIVerifier(), NLIVerifier)


def test_sigmoid_squashes_logits_to_probabilities():
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert 0.0 < _sigmoid(-8) < _sigmoid(0) < _sigmoid(8) < 1.0


# ---- live model behavior (gated: needs the ONNX models loadable) ------------


def _models_available() -> bool:
    try:
        from perpleximanus.retrieval.local_encoders import _reranker

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


@live
def test_nli_scores_a_supported_claim_above_an_unrelated_one():
    nli = FastEmbedNLIVerifier()
    supported = nli.score("Paris is the capital of France.", "The capital of France is Paris.")
    unrelated = nli.score("Bananas are rich in potassium.", "The capital of France is Paris.")
    assert supported > unrelated
    verdict = nli.entail("Paris is the capital of France.", "The capital of France is Paris.")
    assert verdict == "entail"

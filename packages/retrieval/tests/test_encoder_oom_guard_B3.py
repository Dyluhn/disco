"""B3 — encoder OOM guard: honest EncoderUnavailable instead of process-kill.

Covers three acceptance cases per the B3 spec:
  (1) LOW RAM   → lazy load raises EncoderUnavailable (not MemoryError / OOM kill)
  (2) AMPLE RAM → loads normally (model constructor mocked, no download), no error
  (3) The reranker's degradation handler re-raises EncoderUnavailable rather than
      silently falling back to input-order (which would give the caller no signal)

No fastembed models are loaded; constructors are monkeypatched with fakes.
No /proc/meminfo is actually read; _mem_available_gb() is monkeypatched.
"""

from __future__ import annotations

import disco.retrieval.local_encoders as le
import pytest
from disco.retrieval.local_encoders import EncoderUnavailable

# ── helpers ────────────────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Drop-in for fastembed.TextEmbedding — records model_name, no download."""

    def __init__(self, *, model_name: str, **kwargs) -> None:
        self.model_name = model_name


class _FakeEncoder:
    """Drop-in for fastembed TextCrossEncoder — records model_name, no download."""

    def __init__(self, *, model_name: str, **kwargs) -> None:
        self.model_name = model_name


# ── fixture: reset singletons before every test ───────────────────────────────


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Each test starts with cleared singletons so lazy-load fires fresh."""
    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setattr(le, "_cross_encoder", None)
    monkeypatch.setattr(le, "_embedding_factory", lambda _model_name: _FakeEmbedder)


# ── (1) LOW RAM → EncoderUnavailable raised, no model instantiated ─────────────


def test_low_ram_embed_raises_encoder_unavailable(monkeypatch):
    """With <1 GB free RAM and full-tier threshold of 4 GB, _embedding() must
    raise EncoderUnavailable before touching the fastembed constructor."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 0.8)  # 0.8 GB << 4.0 GB
    # fastembed constructor is NOT patched — if it were called, it would try a
    # real download and the test would fail for a different (and messier) reason.

    with pytest.raises(EncoderUnavailable, match="encoder load aborted"):
        le._embedding()

    # Singleton must stay None — model was never instantiated.
    assert le._embedding_model is None


def test_low_ram_reranker_raises_encoder_unavailable(monkeypatch):
    """Same for _reranker(): low RAM → EncoderUnavailable, no cross-encoder created."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 0.8)

    with pytest.raises(EncoderUnavailable, match="encoder load aborted"):
        le._reranker()

    assert le._cross_encoder is None


def test_low_ram_lite_tier_uses_lower_threshold(monkeypatch):
    """PMX_ENCODER_TIER=lite lowers the threshold to 0.5 GB; 0.6 GB must pass."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 0.6)  # 0.6 GB > 0.5 GB threshold
    # Should NOT raise — 0.6 GB is above the 0.5 GB lite threshold.
    instance = le._embedding()
    assert instance.model_name == le.EMBED_MODEL_LITE


def test_low_ram_lite_tier_below_threshold_raises(monkeypatch):
    """PMX_ENCODER_TIER=lite, 0.3 GB free (< 0.5 GB lite threshold) → raises."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 0.3)

    with pytest.raises(EncoderUnavailable, match="encoder load aborted"):
        le._embedding()


def test_error_message_contains_actionable_hint(monkeypatch):
    """The EncoderUnavailable message must mention the model name and the knob."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 0.8)

    with pytest.raises(EncoderUnavailable) as exc_info:
        le._embedding()

    msg = str(exc_info.value)
    assert "GB" in msg  # threshold + available in GBs
    assert "DISCO_ENCODER_TIER" in msg  # the operator knows how to fix it
    assert "lite" in msg


# ── (2) AMPLE RAM → normal load, no error ─────────────────────────────────────


def test_ample_ram_embed_loads_normally(monkeypatch):
    """With RAM well above the full-tier 4 GB threshold, _embedding() instantiates
    the model normally (fastembed constructor mocked — no download)."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 32.0)  # 32 GB >> 4 GB
    instance = le._embedding()

    assert isinstance(instance, _FakeEmbedder)
    assert instance.model_name == le.EMBED_MODEL


def test_ample_ram_reranker_loads_normally(monkeypatch):
    """With RAM well above threshold, _reranker() instantiates normally."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 32.0)
    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder)

    instance = le._reranker()

    assert isinstance(instance, _FakeEncoder)
    assert instance.model_name == le.RERANK_MODEL


def test_ample_ram_no_encoder_unavailable_raised(monkeypatch):
    """Verify no EncoderUnavailable is raised when RAM is ample."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: 16.0)
    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder)

    # Neither call should raise.
    embed = le._embedding()
    rerank = le._reranker()

    assert embed is not None
    assert rerank is not None


# ── (3) reranker degradation handler re-raises EncoderUnavailable ─────────────


@pytest.mark.asyncio
async def test_reranker_propagates_encoder_unavailable(monkeypatch):
    """FastEmbedReranker.rerank() must NOT swallow EncoderUnavailable with its
    generic degradation handler — it must re-raise so the WS path can emit an
    honest error frame rather than silently returning unranked results."""
    from disco.retrieval.local_encoders import FastEmbedReranker
    from disco.retrieval.models import Passage

    def _raise_unavailable():
        raise EncoderUnavailable("encoder load aborted: 0.8 GB RAM available, need ~4 GB")

    # Patch _reranker so the lazy load raises inside the thread.
    monkeypatch.setattr(le, "_reranker", _raise_unavailable)

    rr = FastEmbedReranker()
    passages = [
        Passage(id="p0", source_url="http://x/0", source_title="t", text="text"),
    ]

    with pytest.raises(EncoderUnavailable):
        await rr.rerank("query", passages, top_k=1)


@pytest.mark.asyncio
async def test_nli_verifier_propagates_encoder_unavailable(monkeypatch):
    """FastEmbedNLIVerifier.score() must not cache a wrong neutral and must
    re-raise EncoderUnavailable so the caller gets an honest error."""
    from disco.retrieval.local_encoders import FastEmbedNLIVerifier

    def _raise_unavailable():
        raise EncoderUnavailable("encoder load aborted")

    monkeypatch.setattr(le, "_nli_backend", _raise_unavailable)

    nli = FastEmbedNLIVerifier()

    with pytest.raises(EncoderUnavailable):
        nli.score("Paris is the capital of France.", "The capital of France is Paris.")

    # The failed score must NOT be cached as a neutral 0.0.
    assert ("Paris is the capital of France.", "The capital of France is Paris.") not in nli._cache


# ── (4) non-Linux / read failure → guard is a no-op ──────────────────────────


def test_proc_meminfo_unavailable_skips_guard(monkeypatch):
    """When /proc/meminfo is unreadable (non-Linux, sandboxed), _mem_available_gb()
    returns inf and the guard is a no-op (no spurious EncoderUnavailable)."""
    monkeypatch.setattr(le, "_mem_available_gb", lambda: float("inf"))
    # Should load normally even without the proc check.
    instance = le._embedding()
    assert isinstance(instance, _FakeEmbedder)

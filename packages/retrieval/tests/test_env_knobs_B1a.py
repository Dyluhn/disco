"""B1a — PMX_EMBED_MODEL / PMX_RERANK_MODEL env knobs.

Model-name is captured by patching fastembed constructors with in-memory fakes,
so no ONNX model is downloaded or loaded.

Coverage:
  - default model id used when env var is unset
  - override model id used when env var is set (monkeypatched)
  - lazy-load singleton is cached (same object returned on second call)
"""

from __future__ import annotations

import disco.retrieval.local_encoders as le
import pytest

# ── fake constructors ─────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Drop-in for fastembed.TextEmbedding — records model_name, no download."""

    def __init__(self, *, model_name: str, **kwargs) -> None:
        self.model_name = model_name


class _FakeEncoder:
    """Drop-in for fastembed.rerank.cross_encoder.TextCrossEncoder — same."""

    def __init__(self, *, model_name: str, **kwargs) -> None:
        self.model_name = model_name


# ── fixture: reset singletons between tests ───────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Each test starts with fresh (None) singletons so env changes take effect."""
    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setattr(le, "_cross_encoder", None)
    monkeypatch.setattr(le, "_embedding_factory", lambda _model_name: _FakeEmbedder)
    # The loader checks free RAM before it instantiates anything; nothing here
    # loads a model, so a small CI host must not fail these tests.
    monkeypatch.setattr(le, "_require_ram", lambda _model_name: None)


# ── PMX_EMBED_MODEL ───────────────────────────────────────────────────────────


def test_embed_default_when_env_unset(monkeypatch):
    """Without PMX_EMBED_MODEL, _embedding() instantiates with EMBED_MODEL default."""
    monkeypatch.delenv("PMX_EMBED_MODEL", raising=False)
    instance = le._embedding()

    assert instance.model_name == le.EMBED_MODEL


def test_embed_override_from_env(monkeypatch):
    """With PMX_EMBED_MODEL=foo/bar, _embedding() instantiates with foo/bar."""
    monkeypatch.setenv("PMX_EMBED_MODEL", "foo/bar")
    instance = le._embedding()

    assert instance.model_name == "foo/bar"


def test_embed_singleton_cached(monkeypatch):
    """Second call to _embedding() returns the cached instance (no re-creation)."""
    monkeypatch.delenv("PMX_EMBED_MODEL", raising=False)
    a = le._embedding()
    b = le._embedding()

    assert a is b


# ── PMX_RERANK_MODEL ──────────────────────────────────────────────────────────


def test_rerank_default_when_env_unset(monkeypatch):
    """Without PMX_RERANK_MODEL, _reranker() instantiates with RERANK_MODEL default."""
    monkeypatch.delenv("PMX_RERANK_MODEL", raising=False)
    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder)

    instance = le._reranker()

    assert instance.model_name == le.RERANK_MODEL


def test_rerank_override_from_env(monkeypatch):
    """With PMX_RERANK_MODEL=foo/bar, _reranker() instantiates with foo/bar."""
    monkeypatch.setenv("PMX_RERANK_MODEL", "foo/bar")
    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder)

    instance = le._reranker()

    assert instance.model_name == "foo/bar"


def test_rerank_singleton_cached(monkeypatch):
    """Second call to _reranker() returns the cached instance (no re-creation)."""
    monkeypatch.delenv("PMX_RERANK_MODEL", raising=False)
    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder)

    a = le._reranker()
    b = le._reranker()

    assert a is b


# --- onnxruntime CPU arena policy (the DR RSS-floor fix) ---------------------


def test_encoder_cpu_arena_disabled_by_default(monkeypatch):
    """The CPU memory arena is OFF by default — it is the DR RSS-floor culprit
    (grows to the concurrent-inference peak, never shrinks). Live-confirmed: arena
    off cut a quick DR's peak 7.0→3.8 GB and let the floor recede 7.0→2.9 GB."""
    monkeypatch.delenv("DISCO_ENCODER_CPU_ARENA", raising=False)
    monkeypatch.delenv("PMX_ENCODER_CPU_ARENA", raising=False)
    from disco.retrieval.local_encoders import _arena_session_kwargs

    assert _arena_session_kwargs() == {"enable_cpu_mem_arena": False}


def test_encoder_cpu_arena_can_be_reenabled(monkeypatch):
    from disco.retrieval.local_encoders import _arena_session_kwargs

    for val in ("on", "true", "1", "yes"):
        monkeypatch.setenv("DISCO_ENCODER_CPU_ARENA", val)
        assert _arena_session_kwargs() == {"enable_cpu_mem_arena": True}
    for val in ("off", "false", "0", "no"):
        monkeypatch.setenv("DISCO_ENCODER_CPU_ARENA", val)
        assert _arena_session_kwargs() == {"enable_cpu_mem_arena": False}

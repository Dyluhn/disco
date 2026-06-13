"""B1a — PMX_EMBED_MODEL / PMX_RERANK_MODEL env knobs.

Model-name is captured by patching fastembed constructors with in-memory fakes,
so no ONNX model is downloaded or loaded.

Coverage:
  - default model id used when env var is unset
  - override model id used when env var is set (monkeypatched)
  - lazy-load singleton is cached (same object returned on second call)
"""

from __future__ import annotations

import pytest

import disco.retrieval.local_encoders as le


# ── fake constructors ─────────────────────────────────────────────────────────


class _FakeEmbedder:
    """Drop-in for fastembed.TextEmbedding — records model_name, no download."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name


class _FakeEncoder:
    """Drop-in for fastembed.rerank.cross_encoder.TextCrossEncoder — same."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name


# ── fixture: reset singletons between tests ───────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Each test starts with fresh (None) singletons so env changes take effect."""
    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setattr(le, "_cross_encoder", None)


# ── PMX_EMBED_MODEL ───────────────────────────────────────────────────────────


def test_embed_default_when_env_unset(monkeypatch):
    """Without PMX_EMBED_MODEL, _embedding() instantiates with EMBED_MODEL default."""
    monkeypatch.delenv("PMX_EMBED_MODEL", raising=False)
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeEmbedder)

    instance = le._embedding()

    assert instance.model_name == le.EMBED_MODEL


def test_embed_override_from_env(monkeypatch):
    """With PMX_EMBED_MODEL=foo/bar, _embedding() instantiates with foo/bar."""
    monkeypatch.setenv("PMX_EMBED_MODEL", "foo/bar")
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeEmbedder)

    instance = le._embedding()

    assert instance.model_name == "foo/bar"


def test_embed_singleton_cached(monkeypatch):
    """Second call to _embedding() returns the cached instance (no re-creation)."""
    monkeypatch.delenv("PMX_EMBED_MODEL", raising=False)
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeEmbedder)

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

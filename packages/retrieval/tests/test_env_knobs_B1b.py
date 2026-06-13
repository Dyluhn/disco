"""B1b — PMX_ENCODER_TIER lite/full knob.

Verifies the three-tier resolution:
  explicit PMX_EMBED_MODEL / PMX_RERANK_MODEL  >  PMX_ENCODER_TIER  >  "full" default

No ONNX models are downloaded; fastembed constructors are monkeypatched with
in-memory fakes that record the model_name (same pattern as test_env_knobs_B1a).

Coverage:
  (1) PMX_ENCODER_TIER=lite  → lite model ids (EMBED_MODEL_LITE / RERANK_MODEL_LITE)
  (2) PMX_ENCODER_TIER=full  → full model ids (EMBED_MODEL / RERANK_MODEL)
  (3) PMX_ENCODER_TIER unset → full model ids (default preserves existing behaviour)
  (4) explicit PMX_EMBED_MODEL wins over tier=lite
  (5) explicit PMX_RERANK_MODEL wins over tier=lite
  (6) singleton still cached after tier-resolved load
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


# ── fixture: reset singletons + patch fastembed for every test ────────────────


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Fresh singletons + fake constructors for every test."""
    monkeypatch.setattr(le, "_embedding_model", None)
    monkeypatch.setattr(le, "_cross_encoder", None)
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeEmbedder)
    monkeypatch.setattr(
        "fastembed.rerank.cross_encoder.TextCrossEncoder", _FakeEncoder
    )
    # Ensure no stale explicit-model vars bleed in from the process env
    monkeypatch.delenv("PMX_EMBED_MODEL", raising=False)
    monkeypatch.delenv("PMX_RERANK_MODEL", raising=False)
    monkeypatch.delenv("PMX_ENCODER_TIER", raising=False)


# ── (1) lite tier ─────────────────────────────────────────────────────────────


def test_lite_tier_embed(monkeypatch):
    """PMX_ENCODER_TIER=lite → embed resolves to EMBED_MODEL_LITE."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")

    instance = le._embedding()

    assert instance.model_name == le.EMBED_MODEL_LITE, (
        f"Expected {le.EMBED_MODEL_LITE!r}, got {instance.model_name!r}"
    )


def test_lite_tier_rerank(monkeypatch):
    """PMX_ENCODER_TIER=lite → reranker resolves to RERANK_MODEL_LITE."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")

    instance = le._reranker()

    assert instance.model_name == le.RERANK_MODEL_LITE, (
        f"Expected {le.RERANK_MODEL_LITE!r}, got {instance.model_name!r}"
    )


# ── (2) full tier ─────────────────────────────────────────────────────────────


def test_full_tier_embed(monkeypatch):
    """PMX_ENCODER_TIER=full → embed resolves to EMBED_MODEL (full-quality)."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "full")

    instance = le._embedding()

    assert instance.model_name == le.EMBED_MODEL


def test_full_tier_rerank(monkeypatch):
    """PMX_ENCODER_TIER=full → reranker resolves to RERANK_MODEL."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "full")

    instance = le._reranker()

    assert instance.model_name == le.RERANK_MODEL


# ── (3) default (tier unset) ──────────────────────────────────────────────────


def test_default_tier_embed_is_full(monkeypatch):
    """PMX_ENCODER_TIER unset → embed defaults to full-quality EMBED_MODEL."""
    # PMX_ENCODER_TIER already deleted by _reset fixture
    instance = le._embedding()

    assert instance.model_name == le.EMBED_MODEL, (
        "Default (unset tier) must preserve full-quality model to avoid "
        "silent quality regression for existing deployments."
    )


def test_default_tier_rerank_is_full(monkeypatch):
    """PMX_ENCODER_TIER unset → reranker defaults to full-quality RERANK_MODEL."""
    instance = le._reranker()

    assert instance.model_name == le.RERANK_MODEL


# ── (4) explicit PMX_EMBED_MODEL wins over tier=lite ─────────────────────────


def test_explicit_embed_wins_over_lite_tier(monkeypatch):
    """PMX_EMBED_MODEL=custom/model + PMX_ENCODER_TIER=lite → custom/model wins."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")
    monkeypatch.setenv("PMX_EMBED_MODEL", "custom/embed-model")

    instance = le._embedding()

    assert instance.model_name == "custom/embed-model"


# ── (5) explicit PMX_RERANK_MODEL wins over tier=lite ────────────────────────


def test_explicit_rerank_wins_over_lite_tier(monkeypatch):
    """PMX_RERANK_MODEL=custom/model + PMX_ENCODER_TIER=lite → custom/model wins."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")
    monkeypatch.setenv("PMX_RERANK_MODEL", "custom/rerank-model")

    instance = le._reranker()

    assert instance.model_name == "custom/rerank-model"


# ── (6) singleton still cached after tier-resolved load ──────────────────────


def test_lite_tier_embed_singleton_cached(monkeypatch):
    """After a tier-resolved load, subsequent calls return the same object."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")

    a = le._embedding()
    b = le._embedding()

    assert a is b


def test_lite_tier_rerank_singleton_cached(monkeypatch):
    """After a tier-resolved rerank load, subsequent calls return the same object."""
    monkeypatch.setenv("PMX_ENCODER_TIER", "lite")

    a = le._reranker()
    b = le._reranker()

    assert a is b

"""ConfigStore — the persisted assignment overlay that makes Settings real.

The catalogue stays code-defined; only the per-role assignment is persisted, and a
missing/corrupt/stale overlay must fail SAFE to the base config (never crash the
router). save() validates keys against the catalogue.
"""

from __future__ import annotations

import json

import pytest
from perpleximanus.core.llm import ConfigStore, ModelRole, default_config


def _store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


def test_load_with_no_file_is_the_base_config(tmp_path):
    cfg = _store(tmp_path).load()
    base = default_config()
    assert cfg.default_model == base.default_model
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == base.model_for(ModelRole.RAG_ANSWERER)


def test_save_then_load_round_trips_and_drives_model_for(tmp_path):
    store = _store(tmp_path)
    # reassign RAG to a different EXISTING key
    store.save_assignments("driver-local", {ModelRole.RAG_ANSWERER: "summarizer-local"})
    cfg = store.load()
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == "summarizer-local"
    # untouched roles keep their base assignment
    assert cfg.model_for(ModelRole.QUERY_REWRITER) == default_config().model_for(
        ModelRole.QUERY_REWRITER
    )
    # the overlay file is real JSON on disk
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["assignments"]["rag_answerer"] == "summarizer-local"


def test_save_rejects_unknown_model_key(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="unknown model"):
        store.save_assignments("driver-local", {ModelRole.RAG_ANSWERER: "gpt-9-ultra"})
    with pytest.raises(ValueError, match="unknown default_model"):
        store.save_assignments("nope", {})


def test_corrupt_overlay_falls_back_to_base(tmp_path):
    (tmp_path / "config.json").write_text("{ not json")
    cfg = _store(tmp_path).load()
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == default_config().model_for(
        ModelRole.RAG_ANSWERER
    )


def test_stale_keys_in_overlay_are_dropped_not_crashed(tmp_path):
    # a model key that no longer exists, and a bogus role -> both ignored safely
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_model": "deleted-model",  # unknown -> base default kept
                "assignments": {"rag_answerer": "deleted-model", "not_a_role": "rag-local"},
            }
        )
    )
    cfg = _store(tmp_path).load()
    base = default_config()
    assert cfg.default_model == base.default_model
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == base.model_for(ModelRole.RAG_ANSWERER)

"""ConfigStore — the persisted config (catalogue + assignments) that makes Settings
real: add/edit/remove a model and reassign a role, and the running system honors it.

`default_config()` seeds it; thereafter the file is authoritative. A missing/corrupt
file falls back to the seed (never crashes the router); an older assignment-only
overlay is still honored. CRUD validates keys and refuses to remove a model in use.
"""

from __future__ import annotations

import json

import pytest
from disco.core.llm import ConfigStore, ModelRole, Requirement, default_config
from disco.core.llm.config import ExtractionSettings, ModelEntry, SearchSettings


def _entry(**kw) -> ModelEntry:
    base = {
        "model_id": "x.gguf",
        "provider": "x",
        "context_window": 8192,
        "base_url": "http://x/v1",
    }
    return ModelEntry(**{**base, **kw})


def _store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


def test_load_with_no_file_is_the_base_config(tmp_path):
    cfg = _store(tmp_path).load()
    base = default_config()
    assert cfg.default_model == base.default_model
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == base.model_for(ModelRole.RAG_ANSWERER)


def test_save_then_load_round_trips_and_drives_model_for(tmp_path):
    store = _store(tmp_path)
    base = default_config()
    # save_assignments REPLACES the set (the app-server merges a patch first); pass
    # the full set with RAG reassigned to a different existing key.
    full = dict(base.assignments)
    full[ModelRole.RAG_ANSWERER] = "summarizer-local"
    store.sections.save_assignments(base.default_model, full)
    cfg = store.load()
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == "summarizer-local"
    assert cfg.model_for(ModelRole.QUERY_REWRITER) == base.model_for(ModelRole.QUERY_REWRITER)
    # the full config is real JSON on disk
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["assignments"]["rag_answerer"] == "summarizer-local"
    assert "models" in written  # full catalogue persisted, not just an overlay


def test_save_rejects_unknown_model_key(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="unknown model"):
        store.sections.save_assignments("driver-local", {ModelRole.RAG_ANSWERER: "gpt-9-ultra"})
    with pytest.raises(ValueError, match="unknown default_model"):
        store.sections.save_assignments("nope", {})


def test_corrupt_overlay_falls_back_to_base(tmp_path):
    (tmp_path / "config.json").write_text("{ not json")
    cfg = _store(tmp_path).load()
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == default_config().model_for(
        ModelRole.RAG_ANSWERER
    )


def test_legacy_procedural_image_gen_provider_migrates_on_load(tmp_path):
    """W-50 P1: an existing config persisted with the now-removed
    `image_gen.provider="procedural"` must NOT crash the load. The `provider` Literal
    no longer admits "procedural", so without the migration `RouterConfig.model_validate`
    raises and load() silently falls back to the SEED — discarding the user's whole
    catalogue. The migration rewrites it to the safe default ("openrouter") so an
    existing config upgrades silently and the user's edits survive."""
    # A FULL config (has "models" key → the model_validate path) carrying both the
    # legacy provider AND a user-added model, so we can prove the catalogue survives.
    data = default_config().model_dump(mode="json")
    data["image_gen"]["provider"] = "procedural"
    data["models"]["my-custom"] = {
        "model_id": "custom.gguf",
        "provider": "custom",
        "context_window": 4096,
        "base_url": "http://custom/v1",
    }
    (tmp_path / "config.json").write_text(json.dumps(data))

    cfg = _store(tmp_path).load()
    # Migrated, not crashed-to-seed: provider upgraded …
    assert cfg.image_gen.provider == "openrouter"
    # … and the user's catalogue edit is intact (proof we did NOT fall back to the seed).
    assert "my-custom" in cfg.models


def test_add_update_remove_model_round_trips(tmp_path):
    store = _store(tmp_path)
    # add
    store.add_model("my-llama", _entry(model_id="llama-3.3.gguf", provider="my-llama"))
    assert "my-llama" in store.load().models
    # adding a duplicate key fails
    with pytest.raises(ValueError, match="already exists"):
        store.add_model("my-llama", _entry(provider="my-llama"))
    # update an existing model
    store.update_model(
        "my-llama", _entry(model_id="llama-3.4.gguf", provider="my-llama", context_window=4096)
    )
    assert store.load().models["my-llama"].context_window == 4096
    # updating a missing model fails
    with pytest.raises(ValueError, match="unknown model"):
        store.update_model("ghost", _entry(provider="ghost"))
    # remove
    store.remove_model("my-llama")
    assert "my-llama" not in store.load().models


def test_manual_vision_pin_round_trips_all_three_states(tmp_path):
    store = _store(tmp_path)
    store.add_model("visual", _entry(provider="visual", vision=None))
    assert store.load().models["visual"].vision is None
    store.update_model("visual", _entry(provider="visual", vision=True))
    assert store.load().models["visual"].vision is True
    assert Requirement.VISION in store.load().models["visual"].capabilities
    store.update_model("visual", _entry(provider="visual", vision=False))
    assert store.load().models["visual"].vision is False
    assert Requirement.VISION not in store.load().models["visual"].capabilities


def test_remove_rejects_a_model_in_use(tmp_path):
    store = _store(tmp_path)
    base = default_config()
    # the default model can't be removed
    with pytest.raises(ValueError, match="default model"):
        store.remove_model(base.default_model)
    # an assigned model can't be removed (rag-local is assigned to rag_answerer)
    with pytest.raises(ValueError, match="assigned to rag_answerer"):
        store.remove_model("rag-local")
    cfg = store.load().model_copy(update={"vision_escalation_model": "driver-overflow"})
    store.save(cfg)
    with pytest.raises(ValueError, match="vision model"):
        store.remove_model("driver-overflow")


def test_added_model_becomes_assignable_and_routes(tmp_path):
    # the whole point: an added model can be assigned and the assignment resolves.
    store = _store(tmp_path)
    store.add_model("my-llama", _entry(model_id="llama.gguf", provider="my-llama"))
    full = dict(default_config().assignments)
    full[ModelRole.RAG_ANSWERER] = "my-llama"
    store.sections.save_assignments(default_config().default_model, full)
    cfg = store.load()
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == "my-llama"
    assert cfg.entry_for("my-llama").base_url == "http://x/v1"


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


# ---------------------------------------------------------------------------
# WALK-07 — save_search / save_extraction null base_url on bundled-tier flip
# ---------------------------------------------------------------------------


def test_save_search_bundled_clears_stale_base_url(tmp_path):
    """Switching to the bundled keyless tier must zero out any persisted base_url
    so a stale searxng LAN address cannot silently re-engage later."""
    store = _store(tmp_path)
    # Simulate a prior searxng selection with a LAN URL
    store.sections.save_search(
        SearchSettings(
            provider="searxng", base_url="http://192.168.1.202:8888", categories="general"
        )
    )
    assert store.load().search.base_url == "http://192.168.1.202:8888"

    # Flip to bundled — even if the caller passes the old URL it must be cleared,
    # and `categories` is searxng-only so it goes with it.
    store.sections.save_search(
        SearchSettings(
            provider="bundled", base_url="http://192.168.1.202:8888", categories="general"
        )
    )
    cfg = store.load()
    assert cfg.search.provider == "bundled"
    assert cfg.search.base_url == ""
    assert cfg.search.categories == ""


def test_save_search_selfhost_preserves_base_url(tmp_path):
    """Switching TO searxng (self-host) must keep the supplied base_url intact."""
    store = _store(tmp_path)
    store.sections.save_search(
        SearchSettings(provider="searxng", base_url="http://192.168.1.202:8888")
    )
    cfg = store.load()
    assert cfg.search.provider == "searxng"
    assert cfg.search.base_url == "http://192.168.1.202:8888"


def test_save_extraction_local_clears_stale_base_url(tmp_path):
    """Switching to the bundled local tier must zero out any persisted base_url
    so a stale crawl4ai LAN address cannot silently re-engage later."""
    store = _store(tmp_path)
    store.sections.save_extraction(
        ExtractionSettings(provider="crawl4ai", base_url="http://192.168.1.237:11235")
    )
    assert store.load().extraction.base_url == "http://192.168.1.237:11235"

    # Flip to bundled — even if the caller passes the old URL it must be cleared
    store.sections.save_extraction(
        ExtractionSettings(provider="local", base_url="http://192.168.1.237:11235")
    )
    cfg = store.load()
    assert cfg.extraction.provider == "local"
    assert cfg.extraction.base_url == ""


def test_save_extraction_selfhost_preserves_base_url(tmp_path):
    """Switching TO crawl4ai (self-host) must keep the supplied base_url intact."""
    store = _store(tmp_path)
    store.sections.save_extraction(
        ExtractionSettings(provider="crawl4ai", base_url="http://192.168.1.237:11235")
    )
    cfg = store.load()
    assert cfg.extraction.provider == "crawl4ai"
    assert cfg.extraction.base_url == "http://192.168.1.237:11235"


# ---- V2/V4 (§2): the startup vision-probe overlay ---------------------------


def test_apply_vision_probe_overlay_adds_vision_on_load(tmp_path, monkeypatch):
    """The process-lifetime probe overlay (installed at startup) makes load() add
    VISION to a table-text-only driver — the probe beats the static table."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    monkeypatch.delenv("DISCO_DRIVER_VISION", raising=False)
    store = ConfigStore(tmp_path / "cfg.json")  # seed = default catalogue
    # Baseline: Qwen driver-local is text-only per the static table.
    assert Requirement.VISION not in store.load().models["driver-local"].capabilities

    store.apply_vision_probe({"driver-local": True})  # what startup installs
    assert Requirement.VISION in store.load().models["driver-local"].capabilities


def test_apply_vision_probe_none_leaves_table_caps(tmp_path, monkeypatch):
    """A None probe entry (unknown / network miss) is ignored — load() falls through
    to the static table, never claiming unproven vision."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    monkeypatch.delenv("DISCO_DRIVER_VISION", raising=False)
    store = ConfigStore(tmp_path / "cfg.json")
    store.apply_vision_probe({"driver-local": None})
    assert Requirement.VISION not in store.load().models["driver-local"].capabilities


# ---- build-kernel legacy normalization --------------------------------------


def _kernel_store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "cfg.json")


def test_build_kernel_default_is_disco(tmp_path):
    assert _kernel_store(tmp_path).load().build_kernel == "disco"


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
def test_save_build_kernel_normalizes_legacy_to_disco(tmp_path, legacy):
    store = _kernel_store(tmp_path)
    store.sections.save_build_kernel(legacy)
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"
    assert store.load().build_kernel == "disco"


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
def test_stale_persisted_build_kernel_normalizes_to_disco_on_load(tmp_path, legacy):
    raw = default_config().model_copy(update={"build_kernel": legacy})
    (tmp_path / "cfg.json").write_text(json.dumps(raw.model_dump(mode="json")))

    assert _kernel_store(tmp_path).load().build_kernel == "disco"
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"


def test_legacy_pi_config_loads_without_losing_sibling_settings(tmp_path):
    raw = default_config().model_copy(
        update={
            "build_kernel": "pi_experimental",
            "default_model": "driver-overflow",
            "assignments": {ModelRole.RAG_ANSWERER: "answerer-local"},
            "sandbox": default_config().sandbox.model_copy(
                update={"backend": "process", "idle_ttl_s": 123}
            ),
            "search": default_config().search.model_copy(update={"provider": "searxng"}),
        }
    )
    (tmp_path / "cfg.json").write_text(json.dumps(raw.model_dump(mode="json")))

    cfg = _kernel_store(tmp_path).load()

    assert cfg.build_kernel == "disco"
    assert cfg.default_model == "driver-overflow"
    assert cfg.assignments[ModelRole.RAG_ANSWERER] == "answerer-local"
    assert cfg.sandbox.backend == "process"
    assert cfg.sandbox.idle_ttl_s == 123
    assert cfg.search.provider == "searxng"


def test_full_config_save_normalizes_legacy_build_kernel(tmp_path):
    store = _kernel_store(tmp_path)
    cfg = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    returned = store.save(cfg)
    assert returned.build_kernel == "disco"
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"
    assert store.load().build_kernel == "disco"


# ---------------------------------------------------------------------------
# L21 — a persisted config that still names the REMOVED `ddgs` search tier
# ---------------------------------------------------------------------------


def test_a_persisted_ddgs_config_loads_as_bundled_without_losing_the_catalogue(tmp_path, caplog):
    """The failure mode this prevents: `ddgs` is no longer in the provider
    Literal, so a raw validate raises — and `ConfigStore.load()` answers a raise
    by falling back to the SEED, silently deleting the user's whole model
    catalogue and every role assignment. The migration keeps the file loadable,
    so nothing but the dead provider name changes."""
    import logging

    store = _store(tmp_path)
    saved = store.save(
        store.load().model_copy(
            update={"models": {"mine": _entry(model_id="mine.gguf")}, "default_model": "mine"}
        )
    )
    assert saved.default_model == "mine"

    # Rewrite the persisted document the way an older install left it.
    raw = json.loads(store.path.read_text())
    raw["search"] = {"provider": "ddgs", "base_url": "http://192.168.1.202:8888"}
    store.path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING, logger="disco.config"):
        cfg = ConfigStore(store.path).load()

    assert cfg.search.provider == "bundled"
    assert cfg.search.base_url == ""
    # The catalogue survived — this is the whole point.
    assert set(cfg.models) == {"mine"} and cfg.default_model == "mine"
    assert [r for r in caplog.records if "ddgs" in r.getMessage()]

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
    store.save_assignments(base.default_model, full)
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
        store.save_assignments("driver-local", {ModelRole.RAG_ANSWERER: "gpt-9-ultra"})
    with pytest.raises(ValueError, match="unknown default_model"):
        store.save_assignments("nope", {})


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


def test_remove_rejects_a_model_in_use(tmp_path):
    store = _store(tmp_path)
    base = default_config()
    # the default model can't be removed
    with pytest.raises(ValueError, match="default model"):
        store.remove_model(base.default_model)
    # an assigned model can't be removed (rag-local is assigned to rag_answerer)
    with pytest.raises(ValueError, match="assigned to rag_answerer"):
        store.remove_model("rag-local")


def test_added_model_becomes_assignable_and_routes(tmp_path):
    # the whole point: an added model can be assigned and the assignment resolves.
    store = _store(tmp_path)
    store.add_model("my-llama", _entry(model_id="llama.gguf", provider="my-llama"))
    full = dict(default_config().assignments)
    full[ModelRole.RAG_ANSWERER] = "my-llama"
    store.save_assignments(default_config().default_model, full)
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


def test_save_search_ddgs_clears_stale_base_url(tmp_path):
    """Switching to the bundled ddgs tier must zero out any persisted base_url
    so a stale searxng LAN address cannot silently re-engage later."""
    store = _store(tmp_path)
    # Simulate a prior searxng selection with a LAN URL
    store.save_search(
        SearchSettings(provider="searxng", base_url="http://192.168.1.202:8888")
    )
    assert store.load().search.base_url == "http://192.168.1.202:8888"

    # Flip to bundled — even if the caller passes the old URL it must be cleared
    store.save_search(
        SearchSettings(provider="ddgs", base_url="http://192.168.1.202:8888")
    )
    cfg = store.load()
    assert cfg.search.provider == "ddgs"
    assert cfg.search.base_url == ""


def test_save_search_selfhost_preserves_base_url(tmp_path):
    """Switching TO searxng (self-host) must keep the supplied base_url intact."""
    store = _store(tmp_path)
    store.save_search(
        SearchSettings(provider="searxng", base_url="http://192.168.1.202:8888")
    )
    cfg = store.load()
    assert cfg.search.provider == "searxng"
    assert cfg.search.base_url == "http://192.168.1.202:8888"


def test_save_extraction_local_clears_stale_base_url(tmp_path):
    """Switching to the bundled local tier must zero out any persisted base_url
    so a stale crawl4ai LAN address cannot silently re-engage later."""
    store = _store(tmp_path)
    store.save_extraction(
        ExtractionSettings(provider="crawl4ai", base_url="http://192.168.1.237:11235")
    )
    assert store.load().extraction.base_url == "http://192.168.1.237:11235"

    # Flip to bundled — even if the caller passes the old URL it must be cleared
    store.save_extraction(
        ExtractionSettings(provider="local", base_url="http://192.168.1.237:11235")
    )
    cfg = store.load()
    assert cfg.extraction.provider == "local"
    assert cfg.extraction.base_url == ""


def test_save_extraction_selfhost_preserves_base_url(tmp_path):
    """Switching TO crawl4ai (self-host) must keep the supplied base_url intact."""
    store = _store(tmp_path)
    store.save_extraction(
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


# ---- build-kernel gate authority (Disco Pi campaign, codex finding #2) -------
#
# The store is the SINGLE authority deciding whether a persisted `pi_experimental`
# build kernel may load/save as ACTIVE. Gate authority used to be split — the
# app-server validated persistence against ITS env while the agent-server decided
# activation against its own — so a stale value could load as active in the wrong
# process. `experimental_enabled` is injected so a test pins the gate explicitly.


def _kernel_store(tmp_path, *, gate: bool) -> ConfigStore:
    return ConfigStore(tmp_path / "cfg.json", experimental_enabled=lambda: gate)


def test_build_kernel_default_is_disco(tmp_path):
    assert _kernel_store(tmp_path, gate=False).load().build_kernel == "disco"


def test_save_build_kernel_normalizes_pi_to_disco_when_gate_off(tmp_path):
    """A direct save of `pi_experimental` with the gate OFF must persist `disco` —
    never write an active experimental value the executor would later honor."""
    store = _kernel_store(tmp_path, gate=False)
    store.save_build_kernel("pi_experimental")
    # Persisted-on-disk value is normalized, not just the in-memory load.
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"
    assert store.load().build_kernel == "disco"


def test_save_build_kernel_keeps_pi_when_gate_on(tmp_path):
    store = _kernel_store(tmp_path, gate=True)
    store.save_build_kernel("pi_experimental")
    assert store.load().build_kernel == "pi_experimental"


def test_stale_persisted_pi_normalizes_to_disco_on_load_when_gate_off(tmp_path):
    """The core finding-#2 case: a file persisted with `pi_experimental` (e.g. by an
    app-server whose env had the gate ON) must LOAD as `disco` in a process whose
    gate is OFF — a dormant value can never load as active in the wrong process."""
    # Write a raw config carrying an active pi_experimental, bypassing the save gate.
    raw = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    (tmp_path / "cfg.json").write_text(json.dumps(raw.model_dump(mode="json")))

    assert _kernel_store(tmp_path, gate=False).load().build_kernel == "disco"


def test_gate_off_load_writes_through_so_a_later_gate_flip_does_not_auto_activate(tmp_path):
    """Finding #4 (write-through): a gated-off `load()` must not just normalize in memory
    — it must PERSIST `disco` over the stale `pi_experimental`. Otherwise the dormant
    value stays on disk and AUTO-ACTIVATES the instant the gate later flips on, with no
    fresh user selection (the env-split hazard). After a gate-off load: the FILE reads
    `disco`, and a SUBSEQUENT gate-ON load returns `disco` (no auto-activation) until the
    user explicitly re-selects."""
    raw = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    (tmp_path / "cfg.json").write_text(json.dumps(raw.model_dump(mode="json")))

    # Gate OFF: in-memory normalized AND written through to disk.
    assert _kernel_store(tmp_path, gate=False).load().build_kernel == "disco"
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"  # stale value erased on disk

    # Gate flips ON later — the stale value is gone, so it does NOT reactivate.
    assert _kernel_store(tmp_path, gate=True).load().build_kernel == "disco"


def test_gate_on_load_honors_a_freshly_persisted_pi_without_rewriting(tmp_path):
    """The gate-ON counterpart: a file carrying `pi_experimental` loaded in a process
    whose gate is OPEN is honored as-is and the file is NOT rewritten (write-through is
    only the gated-OFF migration). So an operator who deploys with the gate on gets the
    selected experimental kernel."""
    raw = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    (tmp_path / "cfg.json").write_text(json.dumps(raw.model_dump(mode="json")))

    assert _kernel_store(tmp_path, gate=True).load().build_kernel == "pi_experimental"
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "pi_experimental"  # untouched under an open gate


def test_full_config_save_normalizes_pi_to_disco_when_gate_off(tmp_path):
    """Finding #4: the PUBLIC full-config `save()` must also gate-normalize — a config
    carrying an active `pi_experimental` saved verbatim while the gate is OFF would
    bypass `save_build_kernel`/`load` normalization and persist a value the executor
    could later honor if the gate flips. Both the on-disk write AND the returned config
    are normalized to `disco`."""
    store = _kernel_store(tmp_path, gate=False)
    cfg = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    returned = store.save(cfg)
    assert returned.build_kernel == "disco"
    written = json.loads((tmp_path / "cfg.json").read_text())
    assert written["build_kernel"] == "disco"
    assert store.load().build_kernel == "disco"


def test_full_config_save_keeps_pi_when_gate_on(tmp_path):
    """The gate-ON counterpart: a deliberate full-config save of `pi_experimental`
    is honored when the experimental gate is open."""
    store = _kernel_store(tmp_path, gate=True)
    cfg = default_config().model_copy(update={"build_kernel": "pi_experimental"})
    assert store.save(cfg).build_kernel == "pi_experimental"
    assert store.load().build_kernel == "pi_experimental"


def test_experimental_kernels_enabled_reads_injected_authority(tmp_path):
    assert _kernel_store(tmp_path, gate=True).experimental_kernels_enabled() is True
    assert _kernel_store(tmp_path, gate=False).experimental_kernels_enabled() is False


def test_default_gate_reads_env(tmp_path, monkeypatch):
    """With no injected predicate the store falls back to the shared core env gate —
    one ambient-env reader, default OFF."""
    # disco_env prepends the DISCO_ prefix to the bare PI_KERNEL_EXPERIMENTAL suffix.
    monkeypatch.delenv("DISCO_PI_KERNEL_EXPERIMENTAL", raising=False)
    monkeypatch.delenv("PMX_PI_KERNEL_EXPERIMENTAL", raising=False)
    store = ConfigStore(tmp_path / "cfg.json")  # no experimental_enabled injected
    assert store.experimental_kernels_enabled() is False
    monkeypatch.setenv("DISCO_PI_KERNEL_EXPERIMENTAL", "1")
    assert store.experimental_kernels_enabled() is True

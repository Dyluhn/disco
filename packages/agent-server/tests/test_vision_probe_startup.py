"""V2/V4 (§2) — the runtime vision-probe activation seam.

`ConversationRuntime.prewarm_vision_probe` runs the async network probe ONCE at
agent-server startup (wired into the lifespan in app.py) and installs the result
as a process-lifetime overlay on the shared ConfigStore, so every per-request
config load reflects a model server's REAL vision modality (llama.cpp
`/props.modalities.vision`, OpenRouter `input_modalities`) over the static table.

Hermetic: `probe_all_vision` is stubbed — no network. The two cases that matter:
  1. a definitive probe (True) on a table-text-only entry → VISION appears on load;
  2. a probe that RAISES never blocks/crashes startup → load() keeps table caps.
"""

from __future__ import annotations

import disco.agent_server.driver_runtime as driver_runtime_mod
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ModelRole, Requirement, SecretBox, SecretStore


def _runtime(tmp_path) -> ConversationRuntime:
    # Default catalogue (driver-local = Qwen, NOT vision-capable per the static
    # table). A non-existent cfg path → load() falls back to the seed catalogue.
    store = SqliteEventStore(":memory:")
    config_store = ConfigStore(tmp_path / "cfg.json")
    secret_store = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("vision-test-secret-32-bytes")
    )
    cfg = config_store.load()
    entry = cfg.models["driver-local"]
    assert entry.base_url is not None
    config_store.approvals.approve_origin(
        entry.base_url,
        f"model:{entry.provider}",
        entry.api_key_env or "",
        secret_store=secret_store,
    )
    return ConversationRuntime(store, config_store=config_store, secret_store=secret_store)


def _driver_has_vision(rt: ConversationRuntime) -> bool:
    cfg = rt._config_store.load()
    key = cfg.model_for(ModelRole.AGENT_DRIVER)
    return Requirement.VISION in cfg.models[key].capabilities


async def test_prewarm_runs_probe_and_overlays_vision(tmp_path, monkeypatch):
    """A definitive probe result (True) on the driver overrides the static table:
    after prewarm, load() advertises VISION on the previously text-only driver."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    monkeypatch.delenv("DISCO_DRIVER_VISION", raising=False)
    rt = _runtime(tmp_path)
    assert _driver_has_vision(rt) is False  # static table: Qwen is text-only

    called: dict[str, object] = {}

    async def _fake_probe(config, *, origin_approved):
        called["config"] = config
        entry = config.models["driver-local"]
        assert entry.base_url is not None
        assert origin_approved(entry.base_url, f"model:{entry.provider}", entry.api_key_env)
        return {"driver-local": True}

    monkeypatch.setattr(driver_runtime_mod, "probe_all_vision_with_approvals", _fake_probe)
    await rt.prewarm_vision_probe()

    assert "config" in called  # the probe was actually invoked at startup
    assert _driver_has_vision(rt) is True  # probe overrode the static table


async def test_prewarm_is_fail_soft_on_probe_error(tmp_path, monkeypatch):
    """A probe that raises must NEVER block/crash startup: prewarm swallows it and
    load() keeps working with the static (table-only) capabilities."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    monkeypatch.delenv("DISCO_DRIVER_VISION", raising=False)
    rt = _runtime(tmp_path)

    async def _boom(config, *, origin_approved):
        assert origin_approved is not None
        raise RuntimeError("probe endpoint exploded")

    monkeypatch.setattr(driver_runtime_mod, "probe_all_vision_with_approvals", _boom)
    await rt.prewarm_vision_probe()  # must not raise

    # still loadable, still table-only (no overlay installed)
    assert _driver_has_vision(rt) is False

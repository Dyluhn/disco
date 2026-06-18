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

import disco.agent_server.runtime as runtime_mod
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ModelRole, Requirement


def _runtime(tmp_path) -> ConversationRuntime:
    # Default catalogue (driver-local = Qwen, NOT vision-capable per the static
    # table). A non-existent cfg path → load() falls back to the seed catalogue.
    store = SqliteEventStore(":memory:")
    config_store = ConfigStore(tmp_path / "cfg.json")
    return ConversationRuntime(store, config_store=config_store)


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

    async def _fake_probe(config):
        called["config"] = config
        return {"driver-local": True}

    monkeypatch.setattr(runtime_mod, "probe_all_vision", _fake_probe)
    await rt.prewarm_vision_probe()

    assert "config" in called  # the probe was actually invoked at startup
    assert _driver_has_vision(rt) is True  # probe overrode the static table


async def test_prewarm_is_fail_soft_on_probe_error(tmp_path, monkeypatch):
    """A probe that raises must NEVER block/crash startup: prewarm swallows it and
    load() keeps working with the static (table-only) capabilities."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    monkeypatch.delenv("DISCO_DRIVER_VISION", raising=False)
    rt = _runtime(tmp_path)

    async def _boom(config):
        raise RuntimeError("probe endpoint exploded")

    monkeypatch.setattr(runtime_mod, "probe_all_vision", _boom)
    await rt.prewarm_vision_probe()  # must not raise

    # still loadable, still table-only (no overlay installed)
    assert _driver_has_vision(rt) is False

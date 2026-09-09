"""First-run seed (development/scripts/seed_config.py) — the out-of-the-box catalogue.

2026-07-09 fresh-install walkthrough: the seed kept the dev catalogue's paid
OpenRouter examples (claude-3.5-sonnet as `driver-overflow`, gemini-3-flash),
so a brand-new user saw models they never added and could not use without a
key — a false affordance. The seed now ships EXACTLY ONE entry: the honest
`driver-unconfigured` placeholder; users add real models through Settings
(the flow that also stores the key)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient

_SEED = Path(__file__).resolve().parents[4] / "development" / "scripts" / "seed_config.py"


def _run_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ConfigStore:
    monkeypatch.setenv("DISCO_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_PROJECTS_ROOT", str(tmp_path / "projects"))
    spec = importlib.util.spec_from_file_location("seed_config", _SEED)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_config"] = mod
    spec.loader.exec_module(mod)
    mod.main()
    return ConfigStore(tmp_path / "config.json")


def test_fresh_seed_ships_only_the_unconfigured_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = _run_seed(monkeypatch, tmp_path).load()
    assert set(cfg.models) == {"driver-unconfigured"}, (
        f"a fresh install must seed EXACTLY the unconfigured driver, got {sorted(cfg.models)} "
        "(pre-seeded paid models the user never added are a false affordance)"
    )
    assert cfg.default_model == "driver-unconfigured"
    assert cfg.assignments == {}


def test_fresh_seed_disables_vision_escalation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The dev default points at the (dropped) or-gemini-3-flash entry; the seed
    # must not ship a dangling reference — None disables escalation cleanly
    # (routing returns None and the vision guard stays hard).
    cfg = _run_seed(monkeypatch, tmp_path).load()
    assert cfg.vision_escalation_model is None


def test_seed_is_idempotent_and_never_overwrites(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _run_seed(monkeypatch, tmp_path)
    cfg = store.load()
    store.save(cfg.model_copy(update={"default_model": "driver-unconfigured"}))
    before = (tmp_path / "config.json").read_text()
    # Second run: file exists → seed must leave it untouched (Settings owns it).
    import seed_config

    seed_config.main()
    assert (tmp_path / "config.json").read_text() == before


def _seeded_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """A running app over a freshly seeded config — the shape a new install has."""
    store = _run_seed(monkeypatch, tmp_path)
    state = ConfigState(
        store=store,
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    return TestClient(create_app(SqliteEventStore(":memory:"), state))


_REAL_MODEL = {
    "id": "prov-ollama-deepseek",
    "model_id": "deepseek-v4-flash:0731",
    "base_url": "https://ollama.com/v1",
    "context_window": 131072,
    "capabilities": ["tool_calling", "json_mode"],
}


def test_placeholder_driver_retires_once_a_real_model_leads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-29: the stand-in stayed in the Model library after setup.

    It has no endpoint, so every assignment to it fails at runtime — an entry
    the user can pick but never use. Once a real model is the default primary,
    nothing points at the stand-in and it goes.
    """
    client = _seeded_client(monkeypatch, tmp_path)
    assert client.post("/api/models", json=_REAL_MODEL).status_code == 201

    resp = client.put("/api/models/assignments", json={"default_model": "prov-ollama-deepseek"})

    assert resp.status_code == 200
    assert [m["id"] for m in client.get("/api/models").json()] == ["prov-ollama-deepseek"]


def test_placeholder_driver_stays_while_a_role_still_points_at_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Removing it out from under a live assignment would strand that role."""
    client = _seeded_client(monkeypatch, tmp_path)
    client.post("/api/models", json=_REAL_MODEL)
    client.put("/api/models/assignments", json={"roles": {"rag_answerer": "driver-unconfigured"}})

    client.put("/api/models/assignments", json={"default_model": "prov-ollama-deepseek"})

    assert "driver-unconfigured" in {m["id"] for m in client.get("/api/models").json()}

"""P3 — sticky last-picked model tests.

1. set_model_override writes the last-selected sidecar.
2. A fresh runtime.get_last_selected_model() returns the written value (survives restart).
3. create_conversation with model_override=None seeds from last-selected when the key
   is valid in cfg.models; falls through to default when not valid.
4. No last pick ever → default_model applies.
"""

from __future__ import annotations

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import DEFAULT_OWNER_ID, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


def _runtime(tmp_path, monkeypatch) -> ConversationRuntime:
    monkeypatch.setenv("PMX_DB", str(tmp_path / "conv.db"))
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )


# ---- write path: set_model_override also writes last-selected sidecar -------


def test_set_model_override_writes_last_selected(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    rt.set_model_override("conv_abc", "or-gpt-oss-120b")

    # Immediately readable from the same runtime
    assert rt.get_last_selected_model() == "or-gpt-oss-120b"


def test_last_selected_survives_restart(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    rt.set_model_override("conv_abc", "or-gpt-oss-120b")

    # Simulate a server restart
    rt2 = _runtime(tmp_path, monkeypatch)
    assert rt2.get_last_selected_model() == "or-gpt-oss-120b"


def test_no_pick_ever_returns_none(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch)
    assert rt.get_last_selected_model() is None


def test_no_db_path_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("PMX_DB", raising=False)
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "c.json"),
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    rt.set_model_override("conv_x", "some-model")
    # In-memory only (no sidecar path) → get returns None (no persistence)
    assert rt.get_last_selected_model() is None


# ---- seed path: conversation create routes ----------------------------------


def _make_app_with_model(tmp_path, monkeypatch, *, model_id: str = "or-test-model"):
    """Build a TestClient with a config that has model_id in the catalogue."""
    from disco.core.llm import ModelEntry, Requirement, RouterConfig

    monkeypatch.setenv("PMX_DB", str(tmp_path / "conv.db"))
    store = SqliteEventStore(":memory:")
    cfg_store = ConfigStore(tmp_path / "config.json")

    # Inject a catalogue entry for the test model
    cfg = RouterConfig(
        models={
            "local-default": ModelEntry(
                model_id="local-q4",
                provider="llamacpp",
                context_window=32768,
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            ),
            model_id: ModelEntry(
                model_id=model_id,
                provider="openrouter",
                context_window=128000,
                capabilities=frozenset({Requirement.TOOL_CALLING}),
                base_url="https://openrouter.ai/api/v1",
            ),
        },
        default_model="local-default",
        assignments={},
    )
    cfg_store.save(cfg)

    rt = ConversationRuntime(
        store,
        config_store=cfg_store,
        secret_store=SecretStore(tmp_path / "s.json", box=SecretBox(None)),
    )
    app = create_app(store, runtime=rt)
    return TestClient(app), rt, store


def test_create_conversation_seeds_last_selected_when_no_override(tmp_path, monkeypatch):
    """A new conversation with no model_override seeds from last-selected."""
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)

    # Prime the last-selected model
    rt.set_last_selected_model("or-test-model")

    resp = client.post(
        "/conversations",
        json={"surface": "build", "owner_id": DEFAULT_OWNER_ID},
    )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]

    # The conversation's model override should be the last-selected model
    assert rt._model_override.get(cid) == "or-test-model"


def test_create_conversation_explicit_override_wins(tmp_path, monkeypatch):
    """An explicit model_override must never be silently replaced by last-selected."""
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)
    rt.set_last_selected_model("or-test-model")

    resp = client.post(
        "/conversations",
        json={
            "surface": "build",
            "owner_id": DEFAULT_OWNER_ID,
            "model_override": "local-default",
        },
    )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]

    # Explicit pick must win
    assert rt._model_override.get(cid) == "local-default"


def test_create_conversation_no_pick_ever_uses_default(tmp_path, monkeypatch):
    """With no prior pick, a new conversation has no forced override (falls to
    RouterConfig.default_model as before)."""
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)

    resp = client.post(
        "/conversations",
        json={"surface": "build", "owner_id": DEFAULT_OWNER_ID},
    )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]

    # No override set (the default model governs)
    assert rt._model_override.get(cid) is None


def test_create_conversation_stale_last_selected_ignored(tmp_path, monkeypatch):
    """A persisted last-selected key that is NOT in cfg.models must be ignored
    (fail-safe: never compose an invalid routing decision)."""
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)

    # Persist a model key that doesn't exist in the catalogue
    rt.set_last_selected_model("or-model-that-was-deleted")

    resp = client.post(
        "/conversations",
        json={"surface": "build", "owner_id": DEFAULT_OWNER_ID},
    )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]

    # Unknown key → no override (falls to default_model)
    assert rt._model_override.get(cid) is None


# ---- GET endpoint -----------------------------------------------------------


def test_get_last_selected_returns_null_initially(tmp_path, monkeypatch):
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)

    resp = client.get("/models/last-selected")
    assert resp.status_code == 200
    assert resp.json() == {"model": None}


def test_get_last_selected_returns_value_after_pick(tmp_path, monkeypatch):
    client, rt, _ = _make_app_with_model(tmp_path, monkeypatch)
    rt.set_last_selected_model("or-test-model")

    resp = client.get("/models/last-selected")
    assert resp.status_code == 200
    assert resp.json() == {"model": "or-test-model"}

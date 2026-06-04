"""app-server config + library API tests.

Headless: Starlette's TestClient drives the ASGI app in-process over a shared
in-memory store. Mirrors the wire contract the frontend's data layer consumes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from perpleximanus.app_server import create_app
from perpleximanus.app_server.config_state import ConfigState
from perpleximanus.core import SqliteEventStore
from perpleximanus.core.llm import ConfigStore


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client(store: SqliteEventStore, tmp_path) -> TestClient:
    # Isolated assignment overlay per test (PUTs persist here, not the repo).
    cfg_state = ConfigState(store=ConfigStore(tmp_path / "config.json"))
    return TestClient(create_app(store, cfg_state))


# ---- models + assignments (the absolute, manual model story) ----------------


def test_models_catalogue_is_cost_and_capability_legible(client):
    models = client.get("/api/models").json()
    by_id = {m["id"]: m for m in models}
    # local default is free + tool-calling capable
    assert by_id["driver-local"]["provider"] == "local"
    assert by_id["driver-local"]["price_in_per_m"] == 0
    assert "tool_calling" in by_id["driver-local"]["capabilities"]
    # the overflow model is the paid openrouter one (cost-legible)
    assert by_id["driver-overflow"]["provider"] == "openrouter"
    assert by_id["driver-overflow"]["price_in_per_m"] == 3
    assert "vision" in by_id["driver-overflow"]["capabilities"]


def test_assignments_default_and_per_role(client):
    a = client.get("/api/models/assignments").json()
    assert a["default_model"] == "driver-local"
    assert a["roles"]["rag_answerer"] == "rag-local"
    assert set(a["roles"]) == {"rag_answerer", "query_rewriter", "summarizer", "nli_verifier"}


def test_assignment_update_is_absolute(client):
    body = {"roles": {"rag_answerer": "driver-overflow"}}
    resp = client.put("/api/models/assignments", json=body)
    assert resp.status_code == 200
    assert resp.json()["roles"]["rag_answerer"] == "driver-overflow"
    # persisted on the next read; other roles untouched
    roles = client.get("/api/models/assignments").json()["roles"]
    assert roles["rag_answerer"] == "driver-overflow"
    assert roles["summarizer"] == "summarizer-local"


# ---- skills + mcp scaffolds -------------------------------------------------


def test_skills_list_and_toggle(client):
    skills = client.get("/api/skills").json()
    web = next(s for s in skills if s["id"] == "web-research")
    assert web["enabled"] is True
    updated = client.put("/api/skills/doc-analysis", json={"enabled": True}).json()
    assert next(s for s in updated if s["id"] == "doc-analysis")["enabled"] is True


def test_mcp_connections(client):
    conns = client.get("/api/mcp").json()
    fs = next(c for c in conns if c["id"] == "fs")
    assert fs["name"] == "Filesystem"
    assert fs["status"] == "connected"


# ---- library: owner-scoped conversation list + delete -----------------------


def test_conversations_are_owner_scoped(client, store):
    store.create_conversation("c1", owner_id="me", title="My RRF question")
    store.create_conversation("c2", owner_id="someone-else", title="Not mine")

    mine = client.get("/api/conversations", params={"owner_id": "me"}).json()
    assert [c["id"] for c in mine] == ["c1"]
    assert mine[0]["title"] == "My RRF question"
    # the other owner's conversation never surfaces
    assert all(c["id"] != "c2" for c in mine)


def test_delete_is_owner_scoped(client, store):
    store.create_conversation("c1", owner_id="me", title="x")
    store.create_conversation("c2", owner_id="other", title="y")

    # cannot delete another owner's conversation
    resp = client.delete("/api/conversations/c2", params={"owner_id": "me"})
    assert resp.json()["deleted"] is False
    # can delete own
    resp = client.delete("/api/conversations/c1", params={"owner_id": "me"})
    assert resp.json()["deleted"] is True
    assert client.get("/api/conversations", params={"owner_id": "me"}).json() == []

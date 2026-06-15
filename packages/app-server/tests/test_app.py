"""app-server config + library API tests.

Headless: Starlette's TestClient drives the ASGI app in-process over a shared
in-memory store. Mirrors the wire contract the frontend's data layer consumes.
"""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState, normalize_openrouter
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def client(store: SqliteEventStore, tmp_path) -> TestClient:
    # Isolated config + secrets per test (PUTs persist here, not the repo). The
    # secret box has a test app secret, so the encrypted-key path is exercised.
    cfg_state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
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


def test_sandbox_config_get_and_put_round_trip(client):
    # default reflects the seed (SandboxSettings default)
    cfg = client.get("/api/sandbox/config").json()
    assert cfg["backend"] in ("local", "gvisor", "process", "podman")
    # select gVisor with a Docker-over-SSH connection (no secrets — keyless tailnet)
    put = client.put(
        "/api/sandbox/config",
        json={
            "backend": "gvisor",
            "docker_socket": "ssh://sandbox@100.81.82.115",
            "podman_url": cfg["podman_url"],
            "runtime": "runsc",
            "image": "pmx-sandbox:base",
            "workspace_root": "/opt/sandbox/workspaces",
        },
    )
    assert put.status_code == 200 and put.json()["backend"] == "gvisor"
    # persisted: a fresh GET reflects the selection
    assert client.get("/api/sandbox/config").json()["docker_socket"] == "ssh://sandbox@100.81.82.115"


def test_projects_storage_config_round_trip(client, tmp_path):
    # default = unset; the UI sees a clear empty state, not a fake-default
    cfg = client.get("/api/projects/storage/config").json()
    assert cfg["projects_root"] == ""
    assert cfg["status"] == "unset"

    # PUT a real directory → status=ok, persists
    put = client.put(
        "/api/projects/storage/config",
        json={"projects_root": str(tmp_path), "status": "unset"},
    )
    assert put.status_code == 200
    assert put.json()["projects_root"] == str(tmp_path)
    assert put.json()["status"] == "ok"

    # persisted
    again = client.get("/api/projects/storage/config").json()
    assert again["projects_root"] == str(tmp_path)
    assert again["status"] == "ok"


def test_projects_storage_config_rejects_bad_path_with_typed_reason(client):
    # missing path → 400 with a TYPED reason the UI can map to a clear error
    bad = client.put(
        "/api/projects/storage/config",
        json={"projects_root": "/definitely/does/not/exist/here", "status": "unset"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["reason"] == "not_found"


def test_projects_storage_config_unset_is_allowed(client):
    # empty string is a valid "unset" — clearing the path is a legitimate user action
    res = client.put(
        "/api/projects/storage/config",
        json={"projects_root": "", "status": "unset"},
    )
    assert res.status_code == 200
    assert res.json()["projects_root"] == ""
    assert res.json()["status"] == "unset"


def test_assignments_default_and_per_role(client):
    a = client.get("/api/models/assignments").json()
    assert a["default_model"] == "driver-local"
    assert a["roles"]["rag_answerer"] == "rag-local"
    # Only the GENERATIVE LLM roles are assignable. nli_verifier is an ENCODER
    # (bundled in-process / remote via the Encoders setting), NOT an LLM-router
    # role — exposing it here would be a false affordance (the assignment is ignored).
    assert set(a["roles"]) == {"rag_answerer", "query_rewriter", "summarizer"}
    assert "nli_verifier" not in a["roles"]


def test_encoders_config_round_trips(client):
    # Default: bundled in-process (local).
    assert client.get("/api/encoders/config").json()["remote"] is False
    # Flip to remote and back — persisted.
    assert client.put("/api/encoders/config", json={"remote": True}).json()["remote"] is True
    assert client.get("/api/encoders/config").json()["remote"] is True
    assert client.put("/api/encoders/config", json={"remote": False}).json()["remote"] is False


def test_tts_config_round_trips(client):
    # Default: feature on, bundled in-process, ratified voices.
    d = client.get("/api/tts/config").json()
    assert d["enabled"] is True
    assert d["provider"] == "bundled"
    assert d["voice_a"] == "af_heart"
    assert d["voice_b"] == "af_bella"
    # Self-host tier: provider=speaches + base_url — persisted.
    put = client.put(
        "/api/tts/config",
        json={
            "enabled": True,
            "provider": "speaches",
            "base_url": "http://localhost:8000",
            "voice_a": "af_heart",
            "voice_b": "af_bella",
        },
    ).json()
    assert put["provider"] == "speaches"
    assert put["base_url"] == "http://localhost:8000"
    # Paid tier: provider=openai + api_key_env (NAME, not the key) + model — persisted.
    paid = client.put(
        "/api/tts/config",
        json={
            "enabled": True,
            "provider": "openai",
            "base_url": "https://api.openai.com",
            "api_key_env": "OPENAI_API_KEY",
            "model": "tts-1",
        },
    ).json()
    assert paid["provider"] == "openai"
    assert paid["api_key_env"] == "OPENAI_API_KEY"
    assert paid["model"] == "tts-1"
    got = client.get("/api/tts/config").json()
    assert got["provider"] == "openai" and got["model"] == "tts-1"
    # An empty voice falls back to the ratified default rather than persisting "".
    back = client.put(
        "/api/tts/config",
        json={"enabled": True, "provider": "bundled", "voice_a": "", "voice_b": ""},
    ).json()
    assert back["voice_a"] == "af_heart"
    assert back["voice_b"] == "af_bella"


def test_model_crud_add_edit_remove(client):
    new = {
        "id": "my-llama",
        "model_id": "llama-3.3-70b.gguf",
        "base_url": "http://192.168.1.50:8080/v1",
        "context_window": 32768,
        "quantization": "Q4_K_M",
        "capabilities": ["tool_calling", "long_context"],
    }
    # add
    r = client.post("/api/models", json=new)
    assert r.status_code == 201
    by_id = {m["id"]: m for m in r.json()}
    assert by_id["my-llama"]["model_id"] == "llama-3.3-70b.gguf"
    assert by_id["my-llama"]["base_url"] == "http://192.168.1.50:8080/v1"
    assert by_id["my-llama"]["provider"] == "local"  # free -> local view
    assert set(by_id["my-llama"]["capabilities"]) == {"tool_calling", "long_context"}
    # duplicate id is rejected
    assert client.post("/api/models", json=new).status_code == 400
    # the new model is assignable, and the assignment sticks
    client.put("/api/models/assignments", json={"roles": {"rag_answerer": "my-llama"}})
    assert client.get("/api/models/assignments").json()["roles"]["rag_answerer"] == "my-llama"
    # edit
    edited = {**new, "context_window": 8192}
    r = client.put("/api/models/my-llama", json=edited)
    assert r.status_code == 200
    assert {m["id"]: m for m in r.json()}["my-llama"]["context_window"] == 8192
    # cannot remove while assigned
    assert client.delete("/api/models/my-llama").status_code == 400
    # reassign away, then remove
    client.put("/api/models/assignments", json={"roles": {"rag_answerer": "rag-local"}})
    r = client.delete("/api/models/my-llama")
    assert r.status_code == 200
    assert "my-llama" not in {m["id"] for m in r.json()}


def test_edit_and_delete_unknown_model_400(client):
    body = {"id": "ghost", "model_id": "x"}
    assert client.put("/api/models/ghost", json=body).status_code == 400
    assert client.delete("/api/models/ghost").status_code == 400


# ---- OpenRouter: normalize + encrypted key ----------------------------------


def test_normalize_openrouter_maps_pricing_and_capabilities():
    raw = [
        {
            "id": "anthropic/claude-3.5-sonnet",
            "name": "Anthropic: Claude 3.5 Sonnet",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "response_format"],
        },
        {"id": "tiny/model", "context_length": 4096, "pricing": {"prompt": "0", "completion": "0"}},
    ]
    out = {m.id: m for m in normalize_openrouter(raw)}
    sonnet = out["anthropic/claude-3.5-sonnet"]
    assert sonnet.price_in_per_m == 3.0 and sonnet.price_out_per_m == 15.0  # /token -> /Mtok
    assert set(sonnet.capabilities) == {"vision", "tool_calling", "json_mode", "long_context"}
    assert out["tiny/model"].price_in_per_m == 0.0
    assert out["tiny/model"].capabilities == []  # small ctx, no tools/vision


def test_openrouter_key_lifecycle(client):
    # not configured initially, but storable (test app secret present)
    s = client.get("/api/openrouter/key").json()
    assert s == {"configured": False, "locked": False, "can_store": True}
    # set -> configured
    s = client.put("/api/openrouter/key", json={"key": "sk-or-v1-abc"}).json()
    assert s["configured"] is True and s["locked"] is False
    # blank key rejected
    assert client.put("/api/openrouter/key", json={"key": "  "}).status_code == 400
    # clear -> gone
    s = client.delete("/api/openrouter/key").json()
    assert s["configured"] is False


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


def test_skills_crud_round_trip(client):
    # Fresh store starts empty (no fixture skills — real .md files now).
    assert client.get("/api/skills").json() == []

    # Create a skill.
    created = client.post(
        "/api/skills",
        json={
            "name": "Yahoo Finance",
            "description": "How to fetch stock data",
            "body": "Use the v8 chart endpoint with a Mozilla User-Agent.",
        },
    )
    assert created.status_code == 201
    skill = created.json()
    assert skill["id"] == "yahoo-finance"
    assert skill["enabled"] is True
    assert "v8 chart endpoint" in skill["body"]

    # It persists on the next list.
    listed = client.get("/api/skills").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Yahoo Finance"

    # Partial update: toggle enabled + edit the body, leave name/description.
    updated = client.put(
        "/api/skills/yahoo-finance",
        json={"enabled": False, "body": "Updated instructions."},
    ).json()
    assert updated["enabled"] is False
    assert updated["body"] == "Updated instructions."
    assert updated["name"] == "Yahoo Finance"  # untouched

    # Delete.
    assert client.delete("/api/skills/yahoo-finance").status_code == 204
    assert client.get("/api/skills").json() == []
    # Deleting a missing skill is a 404.
    assert client.delete("/api/skills/yahoo-finance").status_code == 404


def test_skills_update_missing_is_404(client):
    assert client.put("/api/skills/nope", json={"enabled": True}).status_code == 404


def test_mcp_connections(client):
    # Rung B (RP-05b): the _mcp fixture list is gone — GET /api/mcp now serves
    # the live pool projection of configured servers, which is empty on a fresh
    # store. CRUD + populated-projection coverage lives in test_mcp_endpoints.py.
    conns = client.get("/api/mcp").json()
    assert isinstance(conns, list)
    assert conns == []


def test_mcp_approval_persists_via_production_default_wiring(store, tmp_path, monkeypatch):
    # REGRESSION (Fable rp-05b-ui REJECT): the DEPLOYED app builds the ASGI app via
    # `__main__.create_app(store)` with NO explicit ConfigState. That default MUST
    # wire the store's shared DB connection, or POST /api/mcp/servers/{name}/approve
    # 500s ("no DB connection for approval persistence") in every real deployment
    # while the fixture-injected endpoint tests (which always pass db_conn) stay
    # green. This test constructs the app exactly as production does (config=None)
    # so the `config or ConfigState(...)` fallback is actually exercised.
    monkeypatch.setenv("PMX_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("PMX_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("PMX_SKILLS_DIR", str(tmp_path / "skills"))

    client = TestClient(create_app(store))  # production construction — no ConfigState

    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "prod",
            "url": "https://mcp.prod.example",
            "transport": "streamable_http",
            "risk_tier": "medium",
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text

    h = "a" * 64
    approved = client.post("/api/mcp/servers/prod/approve", json={"description_hash": h})
    assert approved.status_code == 200, approved.text  # was a 500 before the fix
    assert approved.json()["description_hash"] == h

    # The approval actually persisted to the shared SQLite mcp_approvals table
    # (the table the core SqliteEventStore schema already creates).
    rows = store._conn.execute(
        "SELECT description_hash FROM mcp_approvals WHERE server = ?", ("prod",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["description_hash"] == h

    # …and the live projection now reports it as approved/connected.
    listed = client.get("/api/mcp").json()
    srv = next(c for c in listed if c["name"] == "prod")
    assert srv["description_hash"] == h
    assert srv["status"] == "connected"


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


from disco.core import ConversationStatus, EventSource, StatusEvent


async def test_conversations_include_status(client, store):
    cid = "c1"
    store.create_conversation(cid, owner_id="me", title="Status Test")

    # Initially status is IDLE (backfilled by read-repair from empty state)
    mine = client.get("/api/conversations", params={"owner_id": "me"}).json()
    assert mine[0]["status"] == "IDLE"

    # Append a status event
    await store.append(
        cid, StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.RUNNING)
    )

    mine = client.get("/api/conversations", params={"owner_id": "me"}).json()
    assert mine[0]["status"] == "RUNNING"

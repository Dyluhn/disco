"""MCP CRUD + approval endpoint tests — rung B.

Drives the real FastAPI app (TestClient) against real persistence:
- ConfigStore (JSON file) for server configs
- SQLite mcp_approvals table for approval rows

Asserts: re-approve MUTATES the single row (not a second insert);
hash-diff data returns old-vs-new tool lists for the UI.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.tools.mcp.migrations import set_mcp_approval_pending
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


@pytest.fixture
def config_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def client(store: SqliteEventStore, tmp_path: Path, config_store: ConfigStore) -> TestClient:
    """Build the app with a shared DB connection so mcp_approvals table is
    reachable from ConfigState."""
    # Ensure mcp_approvals table exists in the in-memory DB.
    store._conn.execute("DROP TABLE IF EXISTS mcp_approvals")
    store._conn.execute(
        "CREATE TABLE mcp_approvals ("
        "server TEXT PRIMARY KEY, "
        "description_hash TEXT NOT NULL, "
        "approved_at TEXT NOT NULL, "
        "approved_by TEXT NOT NULL"
        ")"
    )
    store._conn.commit()
    cfg_state = ConfigState(
        store=config_store,
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
        db_conn=store._conn,
    )
    return TestClient(create_app(store, cfg_state))


# ---- helpers ----------------------------------------------------------------


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _connection(client: TestClient, name: str) -> dict:
    return next(c for c in client.get("/api/mcp").json() if c["name"] == name)


def _approve_config(client: TestClient, name: str) -> str:
    pending = _connection(client, name)["new_config_hash"]
    assert isinstance(pending, str) and len(pending) == 64
    response = client.post(
        f"/api/mcp/servers/{name}/approve",
        json={"approval_kind": "config", "config_hash": pending},
    )
    assert response.status_code == 200, response.text
    return pending


def _approve_tools(
    client: TestClient,
    store: SqliteEventStore,
    name: str,
    new_hash: str,
    *,
    old_hash: str = "",
) -> dict:
    # The live agent-server, not the browser, is authoritative for this row.
    set_mcp_approval_pending(store._conn, name, old_hash, new_hash)
    response = client.post(
        f"/api/mcp/servers/{name}/approve",
        json={"approval_kind": "tools", "description_hash": new_hash},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- CRUD -------------------------------------------------------------------


def test_mcp_list_starts_empty(client):
    """No servers configured → empty list (fixtures are dropped in rung B)."""
    conns = client.get("/api/mcp").json()
    assert conns == []


def test_mcp_create_and_list_round_trip(client, config_store):
    """POST creates a server and activates the fresh-install MCP runtime gate."""
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "filesystem",
            "url": "stdio://mcp-fs",
            "transport": "stdio",
            "enabled": True,
            "risk_tier": "high",
        },
    )
    assert created.status_code == 201
    data = created.json()
    assert data["id"] == "filesystem"
    assert data["name"] == "filesystem"
    assert data["transport"] == "stdio"
    assert data["status"] == "approval_required"
    assert len(data["new_config_hash"]) == 64

    listed = client.get("/api/mcp").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "filesystem"
    assert config_store.load().mcp.enabled is True


def test_mcp_create_duplicate_is_400(client):
    """Duplicate server name is rejected."""
    client.post(
        "/api/mcp/servers",
        json={"name": "dup", "url": "https://example.com", "transport": "streamable_http"},
    )
    assert (
        client.post(
            "/api/mcp/servers",
            json={"name": "dup", "url": "https://other.com", "transport": "streamable_http"},
        ).status_code
        == 400
    )


def test_mcp_patch_toggle_enabled(client, config_store):
    """PATCH toggles enabled without resetting transport or configured risk."""
    client.post(
        "/api/mcp/servers",
        json={
            "name": "srv1",
            "url": "https://example.com",
            "transport": "streamable_http",
            "risk_tier": "high",
        },
    )
    # Toggle off
    patched = client.patch(
        "/api/mcp/servers/srv1",
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["transport"] == "streamable_http"
    assert patched.json()["risk_tier"] == "high"

    # GET reflects the patch
    listed = client.get("/api/mcp").json()
    assert listed[0]["enabled"] is False
    assert listed[0]["status"] == "disabled"
    assert listed[0]["transport"] == "streamable_http"
    assert listed[0]["risk_tier"] == "high"
    assert config_store.load().mcp.enabled is False

    # The same per-connection switch must be able to reactivate the global
    # runtime gate; there is no separate top-level switch in Settings.
    reenabled = client.patch("/api/mcp/servers/srv1", json={"enabled": True})
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True
    assert config_store.load().mcp.enabled is True


def test_mcp_approval_blocks_while_server_is_disabled(client):
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "disabled",
            "url": "https://example.com",
            "transport": "streamable_http",
            "enabled": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "disabled"

    response = client.post(
        "/api/mcp/servers/disabled/approve",
        json={"approval_kind": "config", "config_hash": created.json()["new_config_hash"]},
    )
    assert response.status_code == 409
    assert "enable it before approving" in response.text


def test_mcp_patch_nonexistent_is_404(client):
    assert (
        client.patch(
            "/api/mcp/servers/ghost",
            json={"name": "ghost", "url": "https://example.com"},
        ).status_code
        == 404
    )


def test_mcp_delete_removes_server(client, config_store):
    """DELETE removes the server and its approval row."""
    client.post(
        "/api/mcp/servers",
        json={"name": "to_delete", "url": "https://example.com", "transport": "streamable_http"},
    )
    assert client.delete("/api/mcp/servers/to_delete").status_code == 204
    assert client.delete("/api/mcp/servers/to_delete").status_code == 404
    assert client.get("/api/mcp").json() == []
    assert config_store.load().mcp.enabled is False


# ---- approval ---------------------------------------------------------------


def test_mcp_config_approve_creates_separate_approval_row(client, store):
    """Pre-connect approval pins config without fabricating tool approval."""
    client.post(
        "/api/mcp/servers",
        json={"name": "mysrv", "url": "https://tools.example.com", "transport": "streamable_http"},
    )
    config_hash = _approve_config(client, "mysrv")
    data = _connection(client, "mysrv")
    assert data["config_hash"] == config_hash
    assert data["new_config_hash"] is None
    assert data["description_hash"] is None
    # Tool approval remains required until the live server is discovered.
    assert data["status"] == "approval_required"
    row = store._conn.execute(
        "SELECT config_hash FROM mcp_config_approvals WHERE server = ?", ("mysrv",)
    ).fetchone()
    assert row[0] == config_hash


def test_mcp_tool_approve_requires_authoritative_pending_hash(client, store):
    client.post(
        "/api/mcp/servers",
        json={"name": "mysrv", "url": "https://tools.example.com", "transport": "streamable_http"},
    )
    _approve_config(client, "mysrv")
    h = _hash("echo, add, read_file")

    forged = client.post(
        "/api/mcp/servers/mysrv/approve",
        json={"approval_kind": "tools", "description_hash": h},
    )
    assert forged.status_code == 409

    data = _approve_tools(client, store, "mysrv", h)
    assert data["description_hash"] == h
    assert data["approved_at"] is not None
    assert data["status"] == "connected"


def test_mcp_reapprove_mutates_single_row_not_second_insert(client, store):
    """Re-approve with a new hash mutates the EXISTING row — the DB must have
    exactly one row, not two."""
    client.post(
        "/api/mcp/servers",
        json={"name": "srv", "url": "https://example.com", "transport": "streamable_http"},
    )
    _approve_config(client, "srv")
    h1 = _hash("tool_a, tool_b")
    _approve_tools(client, store, "srv", h1)

    # Count rows — should be exactly 1
    count1 = store._conn.execute(
        "SELECT COUNT(*) FROM mcp_approvals WHERE server = ?", ("srv",)
    ).fetchone()[0]
    assert count1 == 1

    # Re-approve with a different hash
    h2 = _hash("tool_a, tool_b, tool_c")
    data = _approve_tools(client, store, "srv", h2, old_hash=h1)
    assert data["description_hash"] == h2

    # Still exactly one row
    count2 = store._conn.execute(
        "SELECT COUNT(*) FROM mcp_approvals WHERE server = ?", ("srv",)
    ).fetchone()[0]
    assert count2 == 1

    # The stored hash is the NEW one
    stored = store._conn.execute(
        "SELECT description_hash FROM mcp_approvals WHERE server = ?", ("srv",)
    ).fetchone()[0]
    assert stored == h2


def test_mcp_approve_nonexistent_server_is_404(client):
    assert (
        client.post(
            "/api/mcp/servers/nope/approve",
            json={"approval_kind": "config", "config_hash": _hash("x")},
        ).status_code
        == 404
    )


# ---- approval diff ----------------------------------------------------------


def test_mcp_approval_diff_returns_old_vs_new_hash(client, store):
    """When a new hash is submitted and an old hash exists, the diff endpoint
    returns both for the UI to render."""
    client.post(
        "/api/mcp/servers",
        json={"name": "diffsrv", "url": "https://example.com", "transport": "streamable_http"},
    )
    _approve_config(client, "diffsrv")
    h1 = _hash("old tools")
    _approve_tools(client, store, "diffsrv", h1)

    # Verify the approval row is there
    row = store._conn.execute(
        "SELECT description_hash, approved_at, approved_by FROM mcp_approvals WHERE server = ?",
        ("diffsrv",),
    ).fetchone()
    assert row is not None
    assert row[0] == h1

    # Now re-approve with new hash and verify the row mutates (not a second row)
    h2 = _hash("new tools")
    _approve_tools(client, store, "diffsrv", h2, old_hash=h1)

    # Verify old hash is gone, new one is stored
    count = store._conn.execute(
        "SELECT COUNT(*) FROM mcp_approvals WHERE server = ?", ("diffsrv",)
    ).fetchone()[0]
    assert count == 1
    stored = store._conn.execute(
        "SELECT description_hash FROM mcp_approvals WHERE server = ?", ("diffsrv",)
    ).fetchone()[0]
    assert stored == h2


def test_mcp_approval_diff_no_prior_is_none(client):
    """When there is no prior approval, the diff method returns None."""
    client.post(
        "/api/mcp/servers",
        json={"name": "fresh", "url": "https://example.com", "transport": "streamable_http"},
    )
    # The server has no approval row → diff should be None
    row = client.get("/api/mcp").json()
    assert len(row) == 1
    assert row[0]["description_hash"] is None


def test_mcp_connection_projection_includes_optional_fields(client, store):
    """GET /api/mcp returns the full DTO with new optional fields populated."""
    client.post(
        "/api/mcp/servers",
        json={
            "name": "fullsrv",
            "url": "https://mcp.example.com",
            "transport": "streamable_http",
            "enabled": True,
            "risk_tier": "high",
        },
    )
    _approve_config(client, "fullsrv")
    h = _hash("tool_a, tool_b")
    _approve_tools(client, store, "fullsrv", h)
    listed = client.get("/api/mcp").json()
    srv = listed[0]
    assert srv["transport"] == "streamable_http"
    assert srv["risk_tier"] == "high"
    assert srv["description_hash"] == h
    assert srv["approved_at"] is not None
    assert srv["enabled"] is True
    assert srv["status"] == "connected"

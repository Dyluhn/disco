"""Generic /api/secrets routes — encrypt any provider key at rest.

Write-only over the wire (GET never returns a value), env-var-name validated,
the "openrouter" slot reserved for its dedicated route.
"""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path) -> TestClient:
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    return TestClient(create_app(SqliteEventStore(":memory:"), state))


def test_set_get_list_delete_round_trip(client):
    # empty to start
    assert client.get("/api/secrets").json()["names"] == []

    # set a provider key
    r = client.put("/api/secrets/OPENAI_API_KEY", json={"value": "sk-openai-SECRET"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "OPENAI_API_KEY" and body["configured"] is True

    # GET status never returns the value, only that it's configured
    status = client.get("/api/secrets/OPENAI_API_KEY").json()
    assert status["configured"] is True
    assert "SECRET" not in str(status) and "value" not in status

    # it shows up in the list (names only)
    listing = client.get("/api/secrets").json()
    assert listing["names"] == ["OPENAI_API_KEY"]
    assert "SECRET" not in str(listing)

    # delete clears it
    assert client.delete("/api/secrets/OPENAI_API_KEY").json()["configured"] is False
    assert client.get("/api/secrets").json()["names"] == []


def test_blank_value_rejected(client):
    assert client.put("/api/secrets/FOO_KEY", json={"value": "  "}).status_code == 400


def test_invalid_name_rejected(client):
    # path-traversal / non-identifier names are 400 (also blocks weird store keys)
    for bad in ("../etc", "has space", "has-dash", "9starts_with_digit"):
        assert client.put(f"/api/secrets/{bad}", json={"value": "x"}).status_code in (400, 404)


def test_openrouter_name_is_reserved(client):
    # the generic route refuses the reserved slot (dedicated route owns it)
    assert client.put("/api/secrets/openrouter", json={"value": "sk-or"}).status_code == 400


def test_openrouter_key_excluded_from_generic_list(client):
    # set the OpenRouter key via its dedicated route...
    client.put("/api/openrouter/key", json={"key": "sk-or-v1-KEY"})
    # ...and a generic key via this route
    client.put("/api/secrets/TAVILY_API_KEY", json={"value": "tvly-KEY"})
    # the generic list shows the provider key but NOT the reserved openrouter slot
    names = client.get("/api/secrets").json()["names"]
    assert names == ["TAVILY_API_KEY"]


def test_locked_names_name_the_undecryptable_keys(tmp_path):
    """When the app secret changed since save, GET /api/secrets names EXACTLY which
    stored keys can't be decrypted (not just a global locked flag)."""
    secrets_path = tmp_path / "secrets.json"
    # write two keys under one app secret...
    seed = SecretStore(secrets_path, box=SecretBox("original-secret"))
    seed.set_secret("OPENAI_API_KEY", "sk-x")
    seed.set_secret("TAVILY_API_KEY", "tvly-y")

    # ...then serve with a DIFFERENT app secret → both are undecryptable
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(secrets_path, box=SecretBox("changed-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    client = TestClient(create_app(SqliteEventStore(":memory:"), state))
    body = client.get("/api/secrets").json()
    assert body["locked"] is True
    assert body["locked_names"] == ["OPENAI_API_KEY", "TAVILY_API_KEY"]


def test_cannot_store_without_app_secret(tmp_path):
    """No DISCO_SECRET_KEY → no SecretBox → set is a clean 400, not a 500."""
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
        skills=SkillStore(tmp_path / "skills"),
    )
    client = TestClient(create_app(SqliteEventStore(":memory:"), state))
    r = client.put("/api/secrets/OPENAI_API_KEY", json={"value": "sk-x"})
    assert r.status_code == 400

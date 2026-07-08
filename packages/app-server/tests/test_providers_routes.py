"""Generic provider routes — first-class provider objects + catalogue toggles."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from disco.app_server import create_app
from disco.app_server.config.dtos import ProviderCatalogueModelDTO, ProviderCreate
from disco.app_server.config_state import ConfigState
from disco.app_server.routes import providers as providers_mod
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


FIXTURES = Path(__file__).parent / "fixtures" / "provider_catalogues"


@pytest.fixture
def state(tmp_path) -> ConfigState:
    return ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )


@pytest.fixture
def client(state) -> TestClient:
    return TestClient(create_app(SqliteEventStore(":memory:"), state))


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_provider_crud_roundtrip_and_secret_is_write_only(client, state, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_fetch(provider, api_key):
        captured["provider"] = provider.id
        captured["api_key"] = api_key
        return []

    monkeypatch.setattr(providers_mod, "_fetch_provider_catalogue", fake_fetch)

    created = client.post(
        "/api/providers",
        json={
            "label": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "kind": "openai-compat",
            "api_key": "sk-openai-secret",
        },
    )

    assert created.status_code == 201
    body = created.json()
    provider = body["provider"]
    assert body["catalogue_ok"] is True
    assert provider["id"] == "openai"
    assert provider["secret_name"] == "provider_openai"
    assert provider["has_key"] is True
    assert captured == {"provider": "openai", "api_key": "sk-openai-secret"}
    assert "sk-openai-secret" not in str(body)
    assert state._secrets.get_secret("provider_openai") == "sk-openai-secret"

    listing = client.get("/api/providers").json()
    assert listing == [provider]
    assert "sk-openai-secret" not in str(listing)

    updated = client.put(
        "/api/providers/openai",
        json={"label": "OpenAI production", "api_key": "sk-openai-rotated"},
    )
    assert updated.status_code == 200
    assert updated.json()["provider"]["label"] == "OpenAI production"
    assert state._secrets.get_secret("provider_openai") == "sk-openai-rotated"

    deleted = client.delete("/api/providers/openai")
    assert deleted.status_code == 204
    assert client.get("/api/providers").json() == []
    assert state._secrets.get_secret("provider_openai") is None


def test_probe_failure_still_saves_provider_and_key(client, state, monkeypatch):
    async def failing_fetch(provider, api_key):
        request = httpx.Request("GET", f"{provider.base_url}/models")
        raise httpx.ConnectError("network down", request=request)

    monkeypatch.setattr(providers_mod, "_fetch_provider_catalogue", failing_fetch)

    resp = client.post(
        "/api/providers",
        json={
            "label": "Relay",
            "base_url": "https://relay.example/v1",
            "kind": "openai-compat",
            "api_key": "sk-relay-secret",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["catalogue_ok"] is False
    assert "network down" in body["catalogue_error"]
    assert client.get("/api/providers").json()[0]["id"] == "relay"
    assert state._secrets.get_secret("provider_relay") == "sk-relay-secret"
    assert "sk-relay-secret" not in str(body)


def test_delete_refuses_while_catalogue_models_reference_provider(client, monkeypatch):
    catalogue = [
        ProviderCatalogueModelDTO(
            model_id="gpt-4o-mini",
            label="GPT-4o mini",
            context_window=128000,
            price_in_per_m=0.15,
            price_out_per_m=0.6,
            capabilities=["long_context"],
        )
    ]

    async def fake_fetch(provider, api_key):
        return catalogue

    monkeypatch.setattr(providers_mod, "_fetch_provider_catalogue", fake_fetch)
    client.post(
        "/api/providers",
        json={
            "label": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "kind": "openai-compat",
            "api_key": "sk-openai",
        },
    )

    enabled = client.post(
        "/api/providers/openai/enable",
        json={"model_id": "gpt-4o-mini", "label": "GPT-4o mini"},
    )
    assert enabled.status_code == 200
    model = next(m for m in enabled.json() if m["api_key_env"] == "provider_openai")
    assert model["model_id"] == "gpt-4o-mini"

    refused = client.delete("/api/providers/openai")
    assert refused.status_code == 409
    assert model["id"] in refused.json()["detail"]
    assert "gpt-4o-mini" in refused.json()["detail"]


def test_enable_creates_catalogue_entry_visible_in_models(client, monkeypatch):
    async def fake_fetch(provider, api_key):
        return [
            ProviderCatalogueModelDTO(
                model_id="anthropic/claude-3.5-sonnet",
                label="Claude 3.5 Sonnet",
                context_window=200000,
                price_in_per_m=3.0,
                price_out_per_m=15.0,
                capabilities=["vision", "long_context"],
            )
        ]

    monkeypatch.setattr(providers_mod, "_fetch_provider_catalogue", fake_fetch)
    client.post(
        "/api/providers",
        json={
            "label": "OpenRouter generic",
            "base_url": "https://openrouter.ai/api/v1",
            "kind": "openai-compat",
            "api_key": "sk-or",
        },
    )
    assert client.get("/api/providers/openrouter-generic/models").status_code == 200

    enabled = client.post(
        "/api/providers/openrouter-generic/enable",
        json={"model_id": "anthropic/claude-3.5-sonnet"},
    )
    assert enabled.status_code == 200

    models = client.get("/api/models").json()
    added = next(m for m in models if m["api_key_env"] == "provider_openrouter-generic")
    assert added["base_url"] == "https://openrouter.ai/api/v1"
    assert added["model_id"] == "anthropic/claude-3.5-sonnet"
    assert added["price_in_per_m"] == 3.0
    assert "vision" in added["capabilities"]

    disabled = client.delete(
        f"/api/providers/openrouter-generic/enable/{added['id']}",
    )
    assert disabled.status_code == 200
    assert added["id"] not in {m["id"] for m in disabled.json()}


def test_normalizers_use_recorded_provider_shapes():
    openrouter = providers_mod._normalize_catalogue("openai-compat", _payload("openrouter"))
    assert openrouter[0].model_id == "anthropic/claude-3.5-sonnet"
    assert openrouter[0].context_window == 200000
    assert openrouter[0].price_in_per_m == 3.0
    assert openrouter[0].price_out_per_m == 15.0
    assert set(openrouter[0].capabilities) == {
        "vision",
        "tool_calling",
        "json_mode",
        "long_context",
    }

    openai = providers_mod._normalize_catalogue("openai-compat", _payload("openai"))
    assert openai[0].model_id == "gpt-4o-mini"
    assert openai[0].context_window is None
    assert openai[0].price_in_per_m is None

    anthropic = providers_mod._normalize_catalogue("anthropic", _payload("anthropic"))
    assert anthropic[0].model_id == "claude-3-5-sonnet-20241022"
    assert anthropic[0].label == "Claude 3.5 Sonnet"

    gemini = providers_mod._normalize_catalogue("gemini", _payload("gemini"))
    assert gemini[0].model_id == "gemini-1.5-pro"
    assert gemini[0].context_window == 1048576
    assert "long_context" in gemini[0].capabilities


@pytest.mark.asyncio
async def test_provider_catalogue_ttl_cache_singleflight(state, monkeypatch):
    state.create_provider(
        ProviderCreate(
            label="OpenAI",
            base_url="https://api.openai.com/v1",
            kind="openai-compat",
            api_key="sk-openai",
        )
    )
    calls = 0

    async def slow_fetch(provider, api_key):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return [
            ProviderCatalogueModelDTO(
                model_id="gpt-4o-mini",
                label="GPT-4o mini",
                context_window=None,
            )
        ]

    monkeypatch.setattr(providers_mod, "_fetch_provider_catalogue", slow_fetch)
    app = create_app(SqliteEventStore(":memory:"), state)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        responses = await asyncio.gather(
            *(ac.get("/api/providers/openai/models") for _ in range(5))
        )
        assert {r.status_code for r in responses} == {200}
        assert calls == 1
        again = await ac.get("/api/providers/openai/models")
        assert again.status_code == 200
        assert calls == 1

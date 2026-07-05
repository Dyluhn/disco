"""T4.1 / T4.2 — the Settings provider-key + data-source test probes.

Each probe makes a REAL network call in production; here the transport helper is
monkeypatched so the unit test exercises the resolution + classification logic
(which endpoint, which key, how the outcome maps to ProbeResult) deterministically
and offline. The live network behaviour is verified separately against OpenRouter.
"""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


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


# ---- T4.1 provider-key probe -------------------------------------------------


def test_key_test_no_endpoint_is_misconfigured(client):
    """A key no configured model references can't be tested → honest misconfigured,
    not a fake green and not a 500."""
    r = client.post("/api/secrets/RANDOM_UNUSED_KEY/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_key_test_points_data_source_keys_elsewhere(client, state):
    """A key used only by a search/extraction provider routes the user to the
    Data sources 'Test connection' probe instead of failing opaquely."""
    # configure a paid search provider that references TAVILY_API_KEY
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="tavily",
            search_base_url="",
            search_api_key_env="TAVILY_API_KEY",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )
    body = client.post("/api/secrets/TAVILY_API_KEY/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"
    assert "Data sources" in body["detail"]


def test_key_test_no_value_stored_is_misconfigured(client):
    """The default config's rewriter model reads DISCO_GEMMA_API_KEY; with no
    value stored the probe says so rather than calling with a None key."""
    body = client.post("/api/secrets/DISCO_GEMMA_API_KEY/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"
    assert "DISCO_GEMMA_API_KEY" in body["detail"]


def test_key_test_ok_when_provider_answers(client, state, monkeypatch):
    """A stored key + a model endpoint that references it + a 200 from the
    provider → ok. The real HTTP call is stubbed; resolution is real."""
    state.set_secret("DISCO_GEMMA_API_KEY", "sk-gemma-live")

    captured: dict = {}

    async def fake_probe(base_url, api_key, model_id):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["model_id"] = model_id
        return True, "ok", "accepted an authenticated completion — the key works"

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_openai_auth", fake_probe)

    body = client.post("/api/secrets/DISCO_GEMMA_API_KEY/test").json()
    assert body["ok"] is True and body["status"] == "ok"
    # it used the model's real base_url + the decrypted key + the model id
    assert captured["base_url"].startswith("http")
    assert captured["api_key"] == "sk-gemma-live"
    assert captured["model_id"]  # the resolved ModelEntry's model_id


def test_key_test_unauthorized_is_not_a_500(client, state, monkeypatch):
    state.set_secret("DISCO_GEMMA_API_KEY", "sk-bad")

    async def fake_probe(base_url, api_key, model_id):
        return False, "unauthorized", "401 — key rejected"

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_openai_auth", fake_probe)
    r = client.post("/api/secrets/DISCO_GEMMA_API_KEY/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["status"] == "unauthorized"


# ---- T4.2 data-source probe --------------------------------------------------


def test_data_source_bundled_is_honest_not_remote_green(client):
    """The default ddgs/local tiers are in-process — reported as 'bundled', never
    as a remote 'ok' that would imply a service answered."""
    body = client.post("/api/data-sources/search/test").json()
    assert body["ok"] is True and body["status"] == "bundled"
    assert body["provider"] == "ddgs"


def test_data_source_new_keyless_search_tiers_are_bundled(client, state):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    for provider in ("arxiv", "semantic_scholar", "site_scoped"):
        state.update_data_sources_config(
            DataSourcesConfigDTO(
                search_provider=provider,
                search_base_url="example.com" if provider == "site_scoped" else "",
                search_api_key_env="",
                extraction_provider="local",
                extraction_base_url="",
                extraction_api_key_env="",
            )
        )
        body = client.post("/api/data-sources/search/test").json()
        assert body["ok"] is True and body["status"] == "bundled"
        assert body["provider"] == provider


def test_data_sources_config_reports_configured_sources(client, state):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.set_secret("TAVILY_API_KEY", "tv-live")
    state.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="searxng",
            search_base_url="http://searx.local:8080",
            search_api_key_env="",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )

    body = client.get("/api/data-sources/config").json()
    assert "searxng" in body["configured_sources"]
    assert "tavily" in body["configured_sources"]


def test_data_source_selfhost_without_url_is_misconfigured(client, state):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="searxng",
            search_base_url="",
            search_api_key_env="",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )
    body = client.post("/api/data-sources/search/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_data_source_selfhost_reachable(client, state, monkeypatch):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="searxng",
            search_base_url="http://searx.local:8080",
            search_api_key_env="",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )

    async def fake_reachable(url, *, api_key=None):
        return True, "ok", f"{url} answered 200 — reachable."

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_reachable", fake_reachable)
    body = client.post("/api/data-sources/search/test").json()
    assert body["ok"] is True and body["status"] == "ok" and body["provider"] == "searxng"


def test_data_source_unknown_kind_is_error_not_500(client):
    r = client.post("/api/data-sources/bogus/test")
    assert r.status_code == 200
    assert r.json()["status"] == "error"

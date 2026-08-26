"""T4.1 / T4.2 — the Settings provider-key + data-source test probes.

Each probe makes a REAL network call in production; here the transport helper is
monkeypatched so the unit test exercises the resolution + classification logic
(which endpoint, which key, how the outcome maps to ProbeResult) deterministically
and offline. The live network behaviour is verified separately against OpenRouter.
"""

from __future__ import annotations

import httpx
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


def _approve_secret_model_origin(state: ConfigState, secret_ref: str) -> None:
    cfg = state._store.load()
    entry = next(e for e in cfg.models.values() if e.api_key_env == secret_ref and e.base_url)
    state.approve_origin(entry.base_url or "", f"model:{entry.provider}", secret_ref)


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
    # configure a paid search provider that references the canonical tavily secret-ref
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.features.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="tavily",
            search_base_url="",
            search_api_key_env="tavily",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )
    body = client.post("/api/secrets/tavily/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"
    assert "Data sources" in body["detail"]


def test_key_test_no_value_stored_is_misconfigured(client, state):
    """The default config's rewriter model reads the gemma secret-ref; with no
    value stored the probe says so rather than calling with a None key."""
    _approve_secret_model_origin(state, "gemma")
    body = client.post("/api/secrets/gemma/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"
    assert "gemma" in body["detail"]


def test_key_test_ok_when_provider_answers(client, state, monkeypatch):
    """A stored key + a model endpoint that references it + a 200 from the
    provider → ok. The real HTTP call is stubbed; resolution is real."""
    state.secrets_admin.set_secret("gemma", "sk-gemma-live")
    _approve_secret_model_origin(state, "gemma")

    captured: dict = {}

    async def fake_probe(base_url, api_key, model_id):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["model_id"] = model_id
        return True, "ok", "accepted an authenticated completion — the key works"

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_openai_auth", fake_probe)

    body = client.post("/api/secrets/gemma/test").json()
    assert body["ok"] is True and body["status"] == "ok"
    # it used the model's real base_url + the decrypted key + the model id
    assert captured["base_url"].startswith("http")
    assert captured["api_key"] == "sk-gemma-live"
    assert captured["model_id"]  # the resolved ModelEntry's model_id


def test_key_test_unauthorized_is_not_a_500(client, state, monkeypatch):
    state.secrets_admin.set_secret("gemma", "sk-bad")
    _approve_secret_model_origin(state, "gemma")

    async def fake_probe(base_url, api_key, model_id):
        return False, "unauthorized", "401 — key rejected"

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_openai_auth", fake_probe)
    r = client.post("/api/secrets/gemma/test")
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

    for provider in ("arxiv", "news", "semantic_scholar", "site_scoped"):
        state.features.update_data_sources_config(
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

    state.secrets_admin.set_secret("TAVILY_API_KEY", "tv-live")
    state.features.update_data_sources_config(
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
    assert "tavily" not in body["configured_sources"]

    state.approve_origin(
        "https://api.tavily.com", "search:tavily", "TAVILY_API_KEY"
    )
    body = client.get("/api/data-sources/config").json()
    assert "tavily" in body["configured_sources"]


def test_active_paid_source_is_not_reported_configured_without_its_key(client, state):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.features.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="tavily",
            search_base_url="",
            search_api_key_env="tavily",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )

    body = client.get("/api/data-sources/config").json()
    assert "tavily" not in body["configured_sources"]


def test_data_sources_save_rejects_non_official_tavily_origin(client):
    response = client.put(
        "/api/data-sources/config",
        json={
            "search_provider": "tavily",
            "search_base_url": "https://proxy.example.test",
            "search_api_key_env": "TAVILY_API_KEY",
            "extraction_provider": "local",
            "extraction_base_url": "",
            "extraction_api_key_env": "",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "invalid_origin"


def test_data_source_selfhost_without_url_is_misconfigured(client, state):
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.features.update_data_sources_config(
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

    state.features.update_data_sources_config(
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


# ---- Brave / Tavily / Firecrawl: real auth shape + real functional call -----
#
# Bug fixed here: the data-source probe used to send `Authorization: Bearer` to
# EVERY paid provider's ROOT URL. Brave actually requires `X-Subscription-Token`
# (Bearer is silently ignored), so the probe never really exercised the stored
# key — a root 200 read as "reachable" even for a garbage key. Each provider now
# gets its own probe that makes ONE real call to the real search/scrape endpoint
# in that vendor's real auth shape, offline-verified below via httpx.MockTransport
# (same fake-transport pattern as retrieval/tests/test_bundled_providers.py).


async def test_probe_brave_search_sends_subscription_token_not_bearer():
    """The Brave probe must authenticate with `X-Subscription-Token` against the
    REAL search endpoint — not `Authorization: Bearer` against the root."""
    from disco.app_server.probe_clients import probe_brave_search

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(
            200, json={"web": {"results": [{"url": "https://example.com", "title": "Example"}]}}
        )

    ok, status, detail = await probe_brave_search(
        "https://api.search.brave.com",
        api_key="brave-live-key",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True and status == "ok"
    assert "/res/v1/web/search" in captured["url"], "must hit the real search endpoint, not root"
    assert captured["headers"].get("x-subscription-token") == "brave-live-key"
    assert "authorization" not in captured["headers"], (
        "Brave does not use Authorization: Bearer — sending it is the original bug"
    )


async def test_probe_brave_search_bad_key_is_credential_failure_not_reachable():
    """A bad Brave key must be exercised against the real endpoint and reported as
    `unauthorized`, never as a bare-root 200 'reachable'."""
    from disco.app_server.probe_clients import probe_brave_search

    def handler(request: httpx.Request) -> httpx.Response:
        # Brave answers 401 when X-Subscription-Token is missing/invalid.
        token = request.headers.get("x-subscription-token")
        if token != "good-key":
            return httpx.Response(401, json={"error": "invalid subscription token"})
        return httpx.Response(200, json={"web": {"results": []}})

    ok, status, detail = await probe_brave_search(
        "https://api.search.brave.com",
        api_key="bad-key",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False and status == "unauthorized"
    assert "rejected" in detail


async def _assert_tavily_search_uses_bearer_and_requires_a_hit():
    """Tavily's current auth shape is Bearer, and a parsed hit is required for green."""
    from disco.app_server.probe_clients import probe_tavily_search

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(
            200, json={"results": [{"url": "https://example.com", "title": "Example"}]}
        )

    ok, status, _ = await probe_tavily_search(
        "https://api.tavily.com",
        api_key="tvly-live",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True and status == "ok"
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"].get("authorization") == "Bearer tvly-live"


async def test_probe_tavily_search_uses_bearer_and_requires_a_hit():
    await _assert_tavily_search_uses_bearer_and_requires_a_hit()


@pytest.mark.asyncio
async def test_probe_tavily_search_puts_key_in_json_body():
    """Historical wire-shape ID now verifies the fail-closed Bearer contract."""
    await _assert_tavily_search_uses_bearer_and_requires_a_hit()


async def test_probe_tavily_search_bad_key_is_credential_failure():
    from disco.app_server.probe_clients import probe_tavily_search

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    ok, status, detail = await probe_tavily_search(
        "https://api.tavily.com", api_key="bad", transport=httpx.MockTransport(handler)
    )
    assert ok is False and status == "unauthorized"


async def test_probe_tavily_rejects_custom_root_as_misconfigured():
    from disco.app_server.probe_clients import probe_tavily_search

    ok, status, detail = await probe_tavily_search(
        "https://proxy.example.test", api_key="probe-secret"
    )
    assert ok is False and status == "misconfigured"
    assert "official API origin" in detail


async def _assert_firecrawl_extract_uses_bearer_on_v2_scrape_endpoint():
    """Firecrawl uses Bearer against V2 and requires a readable passage."""
    from disco.app_server.probe_clients import probe_firecrawl_extract

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "A readable passage with enough words for citation. " * 3,
                    "metadata": {"title": "Example"},
                },
            },
        )

    ok, status, _ = await probe_firecrawl_extract(
        "https://api.firecrawl.dev",
        api_key="fc-live",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True and status == "ok"
    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert captured["headers"].get("authorization") == "Bearer fc-live"


async def test_probe_firecrawl_extract_uses_bearer_on_v2_scrape_endpoint():
    await _assert_firecrawl_extract_uses_bearer_on_v2_scrape_endpoint()


@pytest.mark.asyncio
async def test_probe_firecrawl_extract_uses_bearer_on_real_scrape_endpoint():
    """Historical endpoint ID retained for the current V2 authenticated probe."""
    await _assert_firecrawl_extract_uses_bearer_on_v2_scrape_endpoint()


async def test_probe_firecrawl_extract_bad_key_is_credential_failure():
    from disco.app_server.probe_clients import probe_firecrawl_extract

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key"})

    ok, status, detail = await probe_firecrawl_extract(
        "https://api.firecrawl.dev", api_key="bad", transport=httpx.MockTransport(handler)
    )
    assert ok is False and status == "unauthorized"


@pytest.mark.parametrize(
    ("probe_name", "response"),
    [
        ("tavily", {"results": []}),
        ("brave", {"web": {"results": []}}),
        ("firecrawl", {"success": True, "data": {"markdown": ""}}),
    ],
)
async def test_paid_probe_empty_200_is_not_green(probe_name, response):
    from disco.app_server.probe_clients import (
        probe_brave_search,
        probe_firecrawl_extract,
        probe_tavily_search,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    probes = {
        "tavily": probe_tavily_search,
        "brave": probe_brave_search,
        "firecrawl": probe_firecrawl_extract,
    }
    root = "https://api.tavily.com" if probe_name == "tavily" else f"https://{probe_name}.example"
    ok, status, detail = await probes[probe_name](
        root,
        api_key="probe-secret",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False and status == "error"
    assert "probe-secret" not in detail


@pytest.mark.parametrize(
    ("probe_name", "response"),
    [
        ("tavily", {"results": [{"url": "https://example.com", "title": "Example"}]}),
        ("brave", {"web": {"results": [{"url": "https://example.com", "title": "Example"}]}}),
        (
            "firecrawl",
            {
                "success": True,
                "data": {
                    "markdown": "A readable passage with enough words for citation. " * 3,
                    "metadata": {"title": "Example"},
                },
            },
        ),
    ],
)
async def test_paid_probe_429_is_not_green(probe_name, response):
    """Settings probes reuse each production adapter but allow only one request.

    A production retry policy would turn this 429 → 200 sequence into a green
    probe. The Settings-specific single-attempt policy must report the first
    rate-limited response honestly instead.
    """
    from disco.app_server.probe_clients import (
        probe_brave_search,
        probe_firecrawl_extract,
        probe_tavily_search,
    )

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response)

    probes = {
        "tavily": probe_tavily_search,
        "brave": probe_brave_search,
        "firecrawl": probe_firecrawl_extract,
    }
    root = "https://api.tavily.com" if probe_name == "tavily" else f"https://{probe_name}.example"
    ok, status, detail = await probes[probe_name](
        root,
        api_key="probe-secret",
        transport=httpx.MockTransport(handler),
    )
    assert calls == 1
    assert ok is False and status == "error"
    assert "rate_limited" in detail


def test_paid_probe_executor_uses_settings_budget():
    """Settings probes use one short-lived slot and one bounded attempt."""
    from disco.app_server.probe_clients import _provider_probe_executor

    for provider in ("brave", "tavily", "firecrawl"):
        executor = _provider_probe_executor(provider, None)
        assert executor.policy.deadline_s == 6.0
        assert executor.policy.max_attempts == 1
        assert executor.concurrency == 1


async def test_paid_probe_redirect_with_usable_body_is_not_green():
    from disco.app_server.probe_clients import probe_brave_search

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/next"},
            json={"web": {"results": [{"url": "https://example.com", "title": "Example"}]}},
        )

    ok, status, _detail = await probe_brave_search(
        "https://api.search.brave.com",
        api_key="probe-secret",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False and status == "error"


# ---- route-level: the Settings probe actually dispatches to the vendor-real probe


def test_data_source_search_brave_dispatches_to_brave_probe(client, state, monkeypatch):
    """The /api/data-sources/search/test route for provider=brave must call the
    Brave-specific probe (real endpoint + real header), not the generic
    root-GET reachability check."""
    from disco.app_server.config.dtos import DataSourcesConfigDTO

    state.secrets_admin.set_secret("brave", "brave-key")
    state.features.update_data_sources_config(
        DataSourcesConfigDTO(
            search_provider="brave",
            search_base_url="",
            search_api_key_env="brave",
            extraction_provider="local",
            extraction_base_url="",
            extraction_api_key_env="",
        )
    )

    captured: dict = {}

    async def fake_brave(base_url, *, api_key=None, transport=None):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return True, "ok", f"{base_url} answered a real search query — the key works."

    import disco.app_server.probe_clients as pc

    monkeypatch.setattr(pc, "probe_brave_search", fake_brave)
    body = client.post("/api/data-sources/search/test").json()
    assert body["ok"] is True and body["status"] == "ok" and body["provider"] == "brave"
    assert captured["base_url"] == "https://api.search.brave.com"
    assert captured["api_key"] == "brave-key"

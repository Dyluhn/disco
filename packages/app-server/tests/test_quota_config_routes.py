"""Owner-scoped quota configuration and status API."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SqliteEventStore
from disco.core.quota import QuotaConfig, SqliteQuotaStore
from fastapi.testclient import TestClient


@pytest.fixture
def configured_client(tmp_path):
    event_store = SqliteEventStore(":memory:")
    quotas = SqliteQuotaStore(tmp_path / "quota.db")
    client = TestClient(create_app(event_store, ConfigState(quota_store=quotas)))
    yield client, quotas
    quotas.close()
    event_store.close()


def _limits(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "window_seconds": 60,
        "max_requests": 20,
        "max_input_tokens": 2_000,
        "max_output_tokens": 1_000,
        "max_total_tokens": 3_000,
    }
    body.update(overrides)
    return body


def test_aggregate_config_is_owner_scoped_and_safe(configured_client) -> None:
    client, quotas = configured_client
    response = client.put("/api/quota/config/app-1?owner_id=owner-a", json=_limits())
    assert response.status_code == 200
    assert response.json() == {
        "audience": "app-1",
        "service": None,
        "source": "configured",
        "limits": _limits(),
        "updated_at": response.json()["updated_at"],
    }
    assert quotas.get_config("owner-a", "app-1") is not None
    assert client.get("/api/quota/config/app-1?owner_id=owner-b").status_code == 404


def test_exact_service_status_uses_exact_then_aggregate_then_default(
    configured_client,
) -> None:
    client, _quotas = configured_client
    client.put("/api/quota/config/app-1?owner_id=owner-a", json=_limits(max_requests=10))

    inherited = client.get("/api/quota/status/app-1?owner_id=owner-a&service=ai.chat")
    assert inherited.status_code == 200
    assert inherited.json()["config"]["source"] == "inherited"
    assert inherited.json()["config"]["service"] == "ai.chat"
    assert inherited.json()["config"]["limits"]["max_requests"] == 10

    exact = client.put(
        "/api/quota/config/app-1?owner_id=owner-a&service=ai.chat",
        json=_limits(max_requests=3),
    )
    assert exact.status_code == 200
    assert exact.json()["service"] == "ai.chat"
    assert (
        client.get("/api/quota/status/app-1?owner_id=owner-a&service=ai.chat").json()["config"][
            "limits"
        ]["max_requests"]
        == 3
    )

    assert (
        client.delete("/api/quota/config/app-1?owner_id=owner-a&service=ai.chat").status_code == 204
    )
    assert (
        client.get("/api/quota/status/app-1?owner_id=owner-a&service=ai.chat").json()["config"][
            "source"
        ]
        == "inherited"
    )
    assert client.delete("/api/quota/config/app-1?owner_id=owner-a").status_code == 204
    assert (
        client.get("/api/quota/status/app-1?owner_id=owner-a&service=ai.chat").json()["config"][
            "source"
        ]
        == "default"
    )


def test_status_reports_current_usage_without_cross_owner_leak(configured_client) -> None:
    client, quotas = configured_client
    quotas.configure(
        owner_id="owner-a",
        audience="app-1",
        service="ai.chat",
        limits=QuotaConfig(window_seconds=60, max_requests=5),
        now=datetime(2026, 7, 11, tzinfo=UTC),
    )
    admission = quotas.reserve(
        owner_id="owner-a",
        audience="app-1",
        service="ai.chat",
        reservation_id="req-1",
        estimated_input_tokens=40,
        estimated_output_tokens=20,
    )
    assert admission.allowed

    status = client.get("/api/quota/status/app-1?owner_id=owner-a&service=ai.chat")
    assert status.status_code == 200
    assert status.json()["usage"]["request_count"] == 1
    assert status.json()["usage"]["input_tokens"] == 40
    other = client.get("/api/quota/status/app-1?owner_id=owner-b&service=ai.chat")
    assert other.status_code == 200
    assert other.json()["usage"]["request_count"] == 0
    assert other.json()["config"]["source"] == "default"


@pytest.mark.parametrize(
    "url,body",
    [
        ("/api/quota/config/app-1?owner_id=owner-a&service=*", _limits()),
        ("/api/quota/config/app-1?owner_id=owner-a&service=ai", _limits()),
        ("/api/quota/config/app-1?owner_id=owner-a", _limits(max_requests=True)),
        (
            "/api/quota/config/app-1?owner_id=owner-a",
            _limits(
                max_requests=None,
                max_input_tokens=None,
                max_output_tokens=None,
                max_total_tokens=None,
            ),
        ),
        ("/api/quota/config/app-1?owner_id=owner-a", {**_limits(), "wildcard": "*"}),
    ],
)
def test_invalid_or_wildcard_configuration_is_rejected_without_persistence(
    configured_client, url: str, body: dict[str, object]
) -> None:
    client, quotas = configured_client
    assert client.put(url, json=body).status_code == 422
    assert quotas.get_config("owner-a", "app-1") is None


def test_default_construction_persists_quota_in_shared_event_database(tmp_path) -> None:
    path = tmp_path / "events.db"
    event_store = SqliteEventStore(path)
    with TestClient(create_app(event_store)) as client:
        response = client.put(
            "/api/quota/config/app-1?owner_id=owner-a",
            json=_limits(max_requests=7),
        )
        assert response.status_code == 200
    event_store.close()

    separate_process_store = SqliteQuotaStore(path)
    try:
        stored = separate_process_store.get_config("owner-a", "app-1")
        assert stored is not None
        assert stored.limits.max_requests == 7
    finally:
        separate_process_store.close()

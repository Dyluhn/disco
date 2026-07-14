from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from disco.agent_server.routes.mcp import make_mcp_router
from disco.tools.mcp import McpServerConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _typed_http(name: str = "typed_http") -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="streamable_http",
        url="https://mcp.example.test/rpc",
        headers={"Authorization": "stored-secret-reference"},
        risk_tier="MEDIUM",
    )


def _runtime(
    servers: dict[str, object],
    *,
    enabled: bool = True,
    statuses: dict[str, str] | None = None,
    connected_http: set[str] | None = None,
):
    mcp = SimpleNamespace(enabled=enabled, servers=servers)
    pool = SimpleNamespace(server_status=lambda: statuses or {})
    runtime = SimpleNamespace(
        _config_store=SimpleNamespace(load=lambda: SimpleNamespace(mcp=mcp)),
        _mcp_pool=pool,
        _mcp_http_clients={name: object() for name in connected_http or set()},
        mcp_approval_state=lambda: {},
    )
    return runtime


def _client(runtime) -> TestClient:
    app = FastAPI()
    app.include_router(make_mcp_router(MagicMock(), runtime))
    return TestClient(app)


def test_mcp_server_list_handles_zero_servers_and_global_disable() -> None:
    assert _client(_runtime({})).get("/api/mcp/servers").json() == {
        "enabled": True,
        "servers": {},
    }
    assert _client(_runtime({}, enabled=False)).get("/api/mcp/servers").json() == {
        "enabled": False,
        "servers": {},
    }


def test_mcp_server_list_normalizes_raw_and_typed_entries_without_secrets() -> None:
    raw_stdio = {
        "transport": "stdio",
        "command": ["server"],
        "env": {"TOKEN": "raw-secret-reference"},
        "risk_tier": "low",
    }
    client = _client(
        _runtime(
            {"raw_stdio": raw_stdio, "typed_http": _typed_http()},
            statuses={"raw_stdio": "connected"},
            connected_http={"typed_http"},
        )
    )

    response = client.get("/api/mcp/servers")

    assert response.status_code == 200
    body = response.json()
    assert body["servers"]["raw_stdio"] == {
        "name": "raw_stdio",
        "transport": "stdio",
        "enabled": True,
        "status": "connected",
        "approval_required": False,
    }
    assert body["servers"]["typed_http"]["status"] == "connected"
    rendered = response.text
    assert "raw-secret-reference" not in rendered
    assert "stored-secret-reference" not in rendered


def test_mcp_server_list_reports_disabled_and_malformed_entries_per_server() -> None:
    disabled = {
        "transport": "stdio",
        "command": ["server"],
        "risk_tier": "low",
        "enabled": False,
    }
    malformed = {
        "transport": "sse",
        "headers": {"Authorization": "must-never-appear"},
        "risk_tier": "medium",
    }
    client = _client(
        _runtime(
            {"disabled": disabled, "broken": malformed},
            statuses={"disabled": "connected"},
        )
    )

    response = client.get("/api/mcp/servers")

    assert response.status_code == 200
    servers = response.json()["servers"]
    assert servers["disabled"]["status"] == "disabled"
    assert servers["broken"] == {
        "name": "broken",
        "enabled": False,
        "status": "error",
        "diagnostic": {
            "code": "invalid_mcp_server_config",
            "fields": ["transport"],
        },
        "approval_required": False,
    }
    assert "must-never-appear" not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        _typed_http("one"),
        {
            "transport": "streamable_http",
            "url": "https://mcp.example.test/rpc",
            "risk_tier": "high",
        },
    ],
)
def test_single_mcp_status_normalizes_typed_and_raw_inputs(raw: object) -> None:
    response = _client(_runtime({"one": raw}, connected_http={"one"})).get(
        "/api/mcp/servers/one/status"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "connected"


def test_single_mcp_status_returns_redacted_typed_diagnostic() -> None:
    response = _client(
        _runtime({"broken": {"transport": "stdio", "env": {"KEY": "secret-ref"}}})
    ).get("/api/mcp/servers/broken/status")

    assert response.status_code == 200
    assert response.json()["diagnostic"]["code"] == "invalid_mcp_server_config"
    assert set(response.json()["diagnostic"]["fields"]) == {"risk_tier"}
    assert "secret-ref" not in response.text

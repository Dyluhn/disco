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
    http_states: dict[str, dict[str, object]] | None = None,
    stdio_diagnostics: dict[str, dict[str, object]] | None = None,
):
    mcp = SimpleNamespace(enabled=enabled, servers=servers)
    pool = SimpleNamespace(
        server_status=lambda: statuses or {},
        server_diagnostics=lambda: stdio_diagnostics or {},
    )
    # 13-B3: `_mcp` is the declared public seam `mcp`, and `mcp_approval_state`
    # is owned by McpManager rather than forwarded by a runtime delegate, so the
    # double holds ONE object supplying both instead of two disagreeing surfaces.
    runtime = SimpleNamespace(
        _config_store=SimpleNamespace(load=lambda: SimpleNamespace(mcp=mcp)),
        mcp=SimpleNamespace(
            _pool=pool,
            _http_clients={name: object() for name in connected_http or set()},
            _http_status={
                **{name: {"status": "connected"} for name in connected_http or set()},
                **(http_states or {}),
            },
            mcp_approval_state=lambda: {},
        ),
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
    globally_disabled = _client(
        _runtime({"one": _typed_http("one")}, enabled=False, connected_http={"one"})
    ).get("/api/mcp/servers")
    assert globally_disabled.json()["servers"]["one"]["status"] == "disabled"


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


def test_stdio_connection_failure_is_typed_error_and_redacted() -> None:
    """BUG 2: a failed stdio server (e.g. a bad/missing command) must carry
    the same {code, attempts, exception_type} diagnostic shape the
    streamable_http transport already gets — not just a bare 'error' status —
    while still never leaking a raw command/secret onto the wire."""
    stdio_srv = {
        "transport": "stdio",
        "command": ["nonexistent-tool", "--with-a-secret-arg=must-not-cross-boundary"],
        "risk_tier": "medium",
    }
    response = _client(
        _runtime(
            {"one": stdio_srv},
            statuses={"one": "error"},
            stdio_diagnostics={
                "one": {
                    "code": "mcp_stdio_connect_failed",
                    "attempts": 1,
                    "exception_type": "FileNotFoundError",
                    "secret": "must-not-cross-boundary",
                }
            },
        )
    ).get("/api/mcp/servers/one/status")

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["diagnostic"] == {
        "code": "mcp_stdio_connect_failed",
        "attempts": 1,
        "exception_type": "FileNotFoundError",
    }
    assert "must-not-cross-boundary" not in response.text


def test_stdio_connection_failure_diagnostic_also_appears_on_list_endpoint() -> None:
    stdio_srv = {
        "transport": "stdio",
        "command": ["nonexistent-tool"],
        "risk_tier": "medium",
    }
    response = _client(
        _runtime(
            {"one": stdio_srv},
            statuses={"one": "error"},
            stdio_diagnostics={
                "one": {"code": "mcp_stdio_connect_failed", "exception_type": "FileNotFoundError"}
            },
        )
    ).get("/api/mcp/servers")

    assert response.status_code == 200
    entry = response.json()["servers"]["one"]
    assert entry["status"] == "error"
    assert entry["diagnostic"] == {
        "code": "mcp_stdio_connect_failed",
        "exception_type": "FileNotFoundError",
    }


def test_http_connection_failure_is_typed_degraded_and_redacted() -> None:
    response = _client(
        _runtime(
            {"one": _typed_http("one")},
            http_states={
                "one": {
                    "status": "degraded",
                    "diagnostic": {
                        "code": "mcp_connection_failed",
                        "attempts": 3,
                        "exception_type": "ConnectError",
                        "secret": "must-not-cross-boundary",
                    },
                }
            },
        )
    ).get("/api/mcp/servers/one/status")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["diagnostic"] == {
        "code": "mcp_connection_failed",
        "attempts": 3,
        "exception_type": "ConnectError",
    }
    assert "must-not-cross-boundary" not in response.text

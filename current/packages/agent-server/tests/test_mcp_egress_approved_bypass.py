"""MCP remote connectivity under the default `filtered` egress posture.

Before this, `compute_mcp_proxy_env` pointed every non-loopback MCP HTTP
connection at 127.0.0.1:8888 whenever DISCO_BUILD_EGRESS was `filtered` — the
compose default. No egress sidecar exists in the shipped stack (the
allowlisting proxy is created per-sandbox, per-run, by the sandbox backends),
so every remote MCP server failed with ConnectionRefused before a single byte
left the container.

The scoped fix: an origin that has passed disco's own operator approval gate
connects DIRECTLY; everything else keeps the proxy posture unchanged. These
tests pin both halves plus the wiring between them — the bypass flag handed to
`compute_mcp_proxy_env` is the SAME boolean that admits the connection, so it
cannot drift into "always bypass".
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from disco.agent_server.mcp_transport import McpHttpConnector, compute_mcp_proxy_env
from disco.core import SecurityRisk
from disco.core.llm import ConfigStore, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.mcp import McpServerConfig
from disco.tools.mcp.http import McpHttpClient

_REMOTE = "https://mcp.example.com/mcp"
_NO_CLIENTS: dict[str, McpHttpClient] = {}


def _server(url: str = _REMOTE, name: str = "remote_srv") -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="streamable_http",
        url=url,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# 1. The policy itself
# ---------------------------------------------------------------------------


def test_unapproved_remote_host_still_gets_the_proxy_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the scoping: without the approval flag nothing
    changes — the filtered posture still points at the sidecar."""

    monkeypatch.delenv("DISCO_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)

    env = compute_mcp_proxy_env(_REMOTE, _NO_CLIENTS)

    assert env is not None
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8888"


def test_approved_remote_host_connects_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("DISCO_MCP_EGRESS_PROXY_REQUIRED", raising=False)

    assert compute_mcp_proxy_env(_REMOTE, _NO_CLIENTS, origin_approved=True) is None


def test_operator_can_force_approved_hosts_back_through_their_own_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (a1) posture stays available as an explicit opt-in for an operator
    who actually runs an orchestrator-side egress proxy."""

    monkeypatch.delenv("DISCO_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)
    monkeypatch.setenv("DISCO_MCP_EGRESS_PROXY_REQUIRED", "1")

    env = compute_mcp_proxy_env(_REMOTE, _NO_CLIENTS, origin_approved=True)

    assert env is not None
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8888"


def test_loopback_and_open_posture_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)
    assert compute_mcp_proxy_env("http://localhost:9000/mcp", _NO_CLIENTS) is None
    assert compute_mcp_proxy_env("http://127.0.0.1:9000/mcp", _NO_CLIENTS) is None

    monkeypatch.setenv("DISCO_BUILD_EGRESS", "open")
    assert compute_mcp_proxy_env(_REMOTE, _NO_CLIENTS) is None


# ---------------------------------------------------------------------------
# 2. The wiring — the bypass is the approval gate, not a second opinion
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Stands in for McpHttpClient; records the proxy_env it was handed."""

    seen: dict[str, str] | None | str = "not-called"

    def __init__(self, **_: object) -> None:
        pass

    async def connect(self, proxy_env: dict[str, str] | None = None) -> None:
        type(self).seen = proxy_env

    async def list_tools(self) -> list[object]:
        return []

    async def close(self) -> None:
        return None


def _connector(approved: bool) -> McpHttpConnector:
    class _Connector(McpHttpConnector):
        def origin_approved(self, name: str, srv: McpServerConfig) -> bool:
            return approved

    return _Connector(
        secret_store=cast(SecretStore, object()),
        config_store=cast(ConfigStore, object()),
        store=cast(SqliteEventStore, object()),
        approval_pending={},
    )


@pytest.mark.asyncio
async def test_connect_hands_the_approval_result_to_the_proxy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from disco.tools.mcp.approval import compute_description_hash

    monkeypatch.delenv("DISCO_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)
    monkeypatch.delenv("DISCO_MCP_EGRESS_PROXY_REQUIRED", raising=False)
    monkeypatch.setattr("disco.tools.mcp.http.McpHttpClient", _RecordingClient)
    _RecordingClient.seen = "not-called"

    srv = _server()
    approvals = {srv.name: compute_description_hash([])}

    await _connector(approved=True).connect(srv.name, srv, approvals)

    # Direct: the approved origin was NOT sent to the absent sidecar.
    assert _RecordingClient.seen is None


@pytest.mark.asyncio
async def test_unapproved_origin_never_reaches_the_network_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What stays blocked: an arbitrary host is refused BEFORE a client exists,
    so the bypass can never be reached for one."""

    monkeypatch.setattr("disco.tools.mcp.http.McpHttpClient", _RecordingClient)
    _RecordingClient.seen = "not-called"

    srv = _server(url="https://evil.example.com/mcp", name="evil_srv")
    connector = _connector(approved=False)

    with pytest.raises(RuntimeError, match="not operator-approved"):
        await connector.connect(srv.name, srv, {})

    assert _RecordingClient.seen == "not-called"
    assert connector.clients == cast(dict[str, Any], {})

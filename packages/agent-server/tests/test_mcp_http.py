"""MCP HTTP transport tests — RP-05 rung B.

Anti-gaming bar (from master brief):
- proxy env HONORED: the outbound httpx request observes HTTP_PROXY/https_proxy
- host NOT in the union is DENIED (403 per sidecar contract)
- "bypass attempt" (fresh AsyncClient ignoring the env) MUST fail
- secret reachable only via the closure, NOT in the sandbox spec
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest
from disco.core import SecurityRisk
from disco.tools.mcp.config import McpServerConfig
from disco.tools.mcp.http import McpHttpClient

# ---------------------------------------------------------------------------
# Fake HTTP MCP server (speaks the streamable-HTTP JSON-RPC protocol)
# ---------------------------------------------------------------------------


class _FakeHttpMCPHandler:
    """A simple HTTP handler that responds to MCP JSON-RPC requests.

    Handles: initialize, notifications/initialized, tools/list, tools/call.
    """

    def __init__(self):
        self.requests = []
        self._session_id = "fake-session-001"

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        # Check for proxy headers
        proxy_seen = any(h in request.headers for h in ("Via", "X-Forwarded-For"))

        try:
            body = json.loads(request.content) if request.content else {}
        except json.JSONDecodeError:
            return httpx.Response(403, json={"error": "invalid json"})

        method = body.get("method", "")
        msg_id = body.get("id")
        params = body.get("params", {})

        if method == "initialize":
            result = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-http-mcp", "version": "1.0.0"},
                },
            }
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "tools/list":
            result = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo back the message via HTTP",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            },
                        },
                        {
                            "name": "search",
                            "description": "Search the web via HTTP MCP",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        },
                    ]
                },
            }
        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if tool_name == "echo":
                msg = args.get("message", "")
                content = [{"type": "text", "text": f"HTTP Echo: {msg}"}]
            elif tool_name == "search":
                content = [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "id": "1",
                                        "title": "Result 1",
                                        "url": "https://example.com/1",
                                    },
                                ]
                            }
                        ),
                    }
                ]
            else:
                content = [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
            result = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": content, "isError": False},
            }
        else:
            result = {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
                "id": msg_id,
            }

        headers = {
            "Content-Type": "application/json",
            "mcp-session-id": self._session_id,
        }
        if proxy_seen:
            headers["X-Proxied"] = "true"
        return httpx.Response(200, json=result, headers=headers)


def _fake_http_server_config(
    name: str = "http_srv",
    url: str = "http://localhost:19999/mcp",
) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="streamable_http",
        url=url,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Fake secrets store (closure-only resolution test)
# ---------------------------------------------------------------------------


class _FakeSecretsStore:
    """Minimal secrets store for closure-only resolution tests."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = dict(secrets or {})

    def get(self, name: str) -> str | None:
        return self._secrets.get(name)

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)


# ---------------------------------------------------------------------------
# Real forward HTTP proxy (egress-routing proof — NOT a config-shape stub)
# ---------------------------------------------------------------------------


class _StubAllowlistingProxy:
    """A REAL forward HTTP proxy, run on a background thread, used to prove the
    client's egress actually traverses a proxy (not a `len(_mounts)` assertion).

    httpx, given `proxy=http://127.0.0.1:<port>`, sends each ``http://`` request in
    absolute-URI form (``GET http://host/path HTTP/1.1``) to us. We parse the target
    host from the request line: if it is in ``allow`` we answer ``200 OK-VIA-PROXY``
    (acting as proxy+origin for the test); otherwise ``403``. Every observed host is
    appended to ``.seen`` so a test can assert the request truly came through us —
    and that a bypassing client never did.
    """

    def __init__(self, allow: set[str]) -> None:
        self.allow = set(allow)
        self.seen: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None, "proxy not started"
        return self._server.server_address[1]

    def start(self) -> None:
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # silence the default stderr spam
                pass

            def _handle(self):
                host = urlparse(self.path).hostname or ""
                proxy.seen.append(host)
                if host in proxy.allow:
                    body = b"OK-VIA-PROXY"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_client_connect_and_list_tools():
    """McpHttpClient connects to a real HTTP server and lists tools.

    Production path: the streamable_http_client is created with a configured
    httpx.AsyncClient; the MCP session initializes and tools/list returns the
    server's tools.
    """
    import httpx

    handler = _FakeHttpMCPHandler()
    transport = httpx.MockTransport(handler)

    config = _fake_http_server_config(url="http://fake/mcp")
    client = McpHttpClient(server=config, call_timeout_s=5.0)

    # Override _build_http_client to use our mock transport
    client._build_http_client = lambda proxy_env=None: httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(5.0)
    )

    try:
        await client.connect()
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "echo" in tool_names
        assert "search" in tool_names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_lifecycle_is_owned_across_start_and_reload_tasks():
    """Startup may connect in one task while Settings reload closes in another.

    The SDK transport owns an AnyIO cancel scope, so its context must be entered
    and exited by one durable owner task even though the public calls originate
    from different tasks.
    """
    import httpx

    handler = _FakeHttpMCPHandler()
    transport = httpx.MockTransport(handler)
    client = McpHttpClient(
        server=_fake_http_server_config(url="http://fake/mcp"),
        call_timeout_s=5.0,
    )
    client._build_http_client = lambda proxy_env=None: httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(5.0)
    )

    async def startup_task() -> None:
        await client.connect()

    await asyncio.create_task(startup_task())
    assert {tool.name for tool in await client.list_tools()} >= {"echo", "search"}
    await client.close()

    assert client._owner_task is None
    assert client._session is None


@pytest.mark.asyncio
async def test_http_client_routes_through_proxy_and_denies_offlist_host():
    """ANTI-GAMING (real routing, not config-shape): the client's outbound httpx
    actually traverses a real allowlisting proxy, which 403-denies a host outside
    the union and lets an allowed host through. A `len(_mounts) > 0` assertion was
    explicitly rejected — this stands up a real proxy and observes the requests.

    Drives `_build_http_client(proxy_env=...)` → httpx routing → the proxy gate.
    """
    proxy = _StubAllowlistingProxy(allow={"allowed-mcp.example"})
    proxy.start()
    try:
        port = proxy.port
        proxy_env = {
            "HTTP_PROXY": f"http://127.0.0.1:{port}",
            "http_proxy": f"http://127.0.0.1:{port}",
            # the proxy is on loopback; never proxy loopback *destinations*, but
            # the remote .example targets below MUST go through it.
            "NO_PROXY": "localhost",
        }
        config = _fake_http_server_config(url="http://allowed-mcp.example/mcp")
        client = McpHttpClient(server=config, call_timeout_s=5.0)

        ac = client._build_http_client(proxy_env=proxy_env)
        # Allowed host → the proxy SAW the request and let it through (200).
        r_ok = await ac.get("http://allowed-mcp.example/health")
        assert r_ok.status_code == 200
        assert r_ok.text == "OK-VIA-PROXY"
        assert "allowed-mcp.example" in proxy.seen  # routing is real, not shape

        # Off-list host → the proxy is the enforcing element: 403 denial.
        r_deny = await ac.get("http://evil-offlist.example/exfil")
        assert r_deny.status_code == 403
        await ac.aclose()

        # Bypass (no proxy_env) → the request does NOT reach the proxy at all;
        # with no route to a non-resolving host it errors. The contrast proves
        # the proxy — not httpx defaults — is what gates egress.
        proxy.seen.clear()
        bypass = client._build_http_client(proxy_env=None)
        with pytest.raises(httpx.RequestError):
            await bypass.get("http://allowed-mcp.example/health")
        assert proxy.seen == []  # the proxy never observed the bypass request
        await bypass.aclose()
    finally:
        proxy.stop()


@pytest.mark.asyncio
async def test_mcp_proxy_env_follows_build_egress_posture(monkeypatch):
    """`ConversationRuntime._mcp_proxy_env()` is governed by PMX_BUILD_EGRESS — the
    SAME single source of truth as the sandbox spec. filtered (the DEFAULT, per
    BP-G10) → a real proxy env block pointing at the egress proxy; explicit open
    → None (direct)."""
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.tools.sandbox._container import EGRESS_PROXY_PORT

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)

    # BP-G10 flipped the DEFAULT build egress to "filtered", so an unset
    # PMX_BUILD_EGRESS now means filtered → a real proxy env (NOT direct).
    monkeypatch.delenv("PMX_BUILD_EGRESS", raising=False)
    assert runtime._mcp._mcp_proxy_env() is not None  # default is now filtered → proxied

    # The direct (None) posture requires an EXPLICIT open.
    monkeypatch.setenv("PMX_BUILD_EGRESS", "open")
    assert runtime._mcp._mcp_proxy_env() is None  # explicit open posture → direct

    monkeypatch.setenv("PMX_BUILD_EGRESS", "filtered")
    monkeypatch.setenv("PMX_MCP_EGRESS_PROXY_HOST", "10.0.0.5")
    env = runtime._mcp._mcp_proxy_env()
    assert env is not None
    assert env["HTTP_PROXY"] == f"http://10.0.0.5:{EGRESS_PROXY_PORT}"
    assert env["https_proxy"] == f"http://10.0.0.5:{EGRESS_PROXY_PORT}"
    assert runtime._mcp._mcp_proxy_env("http://localhost:9123/mcp") is None
    assert runtime._mcp._mcp_proxy_env("http://127.0.0.9:9123/mcp") is None
    assert runtime._mcp._mcp_proxy_env("http://[::1]:9123/mcp") is None
    assert runtime._mcp._mcp_proxy_env("http://10.0.0.9:9123/mcp") is not None
    assert runtime._mcp._mcp_proxy_env("https://remote.example/mcp") is not None
    store.close()


def test_secret_absent_from_build_sandbox_spec(monkeypatch):
    """SECRET NEVER CROSSES INTO THE SANDBOX (real spec construction, not a vacuous
    `not hasattr`): an MCP HTTP server configured with a secret-bearing auth header
    is registered on a real ConversationRuntime; we build the ACTUAL build-sandbox
    spec the sandbox would receive and assert the resolved secret value is absent
    from its FULL serialization — while the MCP host IS unioned into egress_allow.

    The control matters: egress hosts DO cross the boundary into the spec, the
    secret does NOT. A test that only asserted "secret absent" against an empty
    spec would pass vacuously; pairing it with "host present" proves the boundary
    is selective, and that we built a spec that actually carries MCP state.
    """
    from disco.agent_server.runtime import ConversationRuntime
    from disco.core import SqliteEventStore

    SECRET = "sk-must-never-serialize-9f3c"
    secrets = _FakeSecretsStore({"api_key": SECRET})
    config = McpServerConfig(
        name="sec_srv",
        transport="streamable_http",
        url="https://secret-mcp.example/mcp",
        headers={"Authorization": "api_key"},
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    client = McpHttpClient(server=config, secrets=secrets, call_timeout_s=5.0)
    # Sanity: the closure DOES resolve the real secret, so its absence below is a
    # meaningful negative (the value genuinely exists at the orchestrator boundary).
    assert client._resolve_headers()["Authorization"] == SECRET

    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(store)
    # Register the client WITHOUT a live connect — _mcp_egress_hosts only reads
    # client._url / client.allowed_hosts, which exist at construction.
    runtime._mcp._http_clients["sec_srv"] = client

    monkeypatch.setenv("PMX_BUILD_EGRESS", "filtered")
    spec = runtime._sandbox._build_sandbox_spec(
        mcp_egress_hosts=runtime._mcp._mcp_egress_hosts()
    )

    blob = spec.model_dump_json()
    assert SECRET not in blob  # the secret never crosses into the sandbox spec
    # ...but the MCP host DID union into the egress allowlist (the boundary is
    # selective, and the spec genuinely carries MCP-derived state).
    assert any("secret-mcp.example" in h for h in spec.egress_allow)
    store.close()


@pytest.mark.asyncio
async def test_http_client_call_tool():
    """Call a tool via the HTTP client and receive a properly shaped result."""
    import httpx

    handler = _FakeHttpMCPHandler()
    transport = httpx.MockTransport(handler)

    config = _fake_http_server_config(url="http://fake/mcp")
    client = McpHttpClient(server=config, call_timeout_s=5.0)
    client._build_http_client = lambda proxy_env=None: httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(5.0)
    )

    try:
        await client.connect()
        result = await client.call_tool("echo", {"message": "hello"})
        assert "content" in result
        assert not result.get("isError", True)
        # Extract the text content
        text = ""
        for item in result["content"]:
            if hasattr(item, "text"):
                text += item.text
        assert "HTTP Echo: hello" in text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_client_init_failure_surfaces_error():
    """Init failure → connect() exception is surfaced so the runtime can
    record status="error" with the captured message.

    PRODUCTION PATH: when the MCP server returns an HTTP error (e.g., 500),
    connect() raises and the runtime catches it to mark the server as error.
    We test by mocking the streamable_http_client itself to simulate a failed
    connection, avoiding anyio-internal race conditions with MockTransport.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    config = _fake_http_server_config(url="http://fail/mcp")

    # Mock the streamable_http_client to raise on __aenter__
    async def _fail_aenter():
        raise httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=MagicMock(status_code=500)
        )

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = _fail_aenter
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "disco.tools.mcp.http.streamable_http_client",
        return_value=mock_ctx,
    ):
        client = McpHttpClient(server=config, call_timeout_s=2.0, init_timeout_s=1.0)

        # noqa: B017 — the mock harness surfaces the failure as a generic
        # Exception (not a narrow type); this asserts only that connect()
        # propagates *some* error so the runtime can record status="error".
        with pytest.raises(Exception):  # noqa: B017
            await client.connect()

        # After failure, session is cleaned up
        assert client._session is None

        await client.close()


@pytest.mark.asyncio
async def test_http_client_configured_timeout():
    """Per-call timeout is configurable (10s default)."""
    config = _fake_http_server_config()
    client = McpHttpClient(server=config, call_timeout_s=10.0)
    assert client._call_timeout_s == 10.0

    client2 = McpHttpClient(server=config, call_timeout_s=30.0)
    assert client2._call_timeout_s == 30.0

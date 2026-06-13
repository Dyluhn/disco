"""MCP Streamable-HTTP transport — RP-05 rung B.

Uses the official mcp SDK's `streamable_http_client` (the modern API, not the
deprecated `streamablehttp_client`). Auth headers are resolved via SecretsStore
in an orchestrator-side CLOSURE — the secret never reaches the sandbox even
though the call is `runs_in="in_process"`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import McpServerConfig

_LOG = logging.getLogger(__name__)


class HttpConnectionClosed(Exception):
    """The HTTP connection was closed (session terminated or transport error)."""


class McpHttpClient:
    """One MCP server over Streamable-HTTP transport.

    Owns an httpx.AsyncClient with proxy settings + auth headers. The client
    is created per-connection; secrets are resolved in a closure so they never
    leak to the sandbox spec.
    """

    def __init__(
        self,
        server: McpServerConfig,
        *,
        secrets: Any | None = None,  # SecretsStore
        init_timeout_s: float = 15.0,
        call_timeout_s: float = 10.0,
    ) -> None:
        if not server.url:
            raise ValueError(
                f"MCP server {server.name!r}: streamable_http requires a url"
            )

        self._name = server.name
        self._url = server.url
        self._headers_config = dict(server.headers or {})
        self._allowed_hosts = list(server.allowed_hosts or [])
        self._secrets = secrets
        self._init_timeout_s = init_timeout_s
        self._call_timeout_s = call_timeout_s

        self._session: ClientSession | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._transport_ctx = None

    # --- helpers ------------------------------------------------------------

    def _resolve_headers(self) -> dict[str, str]:
        """Resolve SecretRefs to plaintext values via the SecretsStore CLOSURE.
        The secret is resolved HERE — never passed to the sandbox."""
        resolved: dict[str, str] = {}
        for key, ref in self._headers_config.items():
            if self._secrets is not None:
                val = self._secrets.get(ref)
                if val is not None:
                    resolved[key] = val
                else:
                    _LOG.debug(
                        "McpHttpClient: secret %r for header %r not found", ref, key
                    )
            else:
                _LOG.debug(
                    "McpHttpClient: no secrets store; skipping header %r", key
                )
        return resolved

    def _build_http_client(
        self,
        proxy_env: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        """Build an httpx.AsyncClient with proxy settings and auth headers.

        The proxy is applied via the `proxies` parameter, honoring HTTP_PROXY /
        HTTPS_PROXY from the provided env dict (the sandbox sidecar proxy block).
        A fresh client without proxy settings would bypass the sidecar — the
        `test_mcp_http` anti-gaming test verifies this."""
        headers = self._resolve_headers()
        proxy_url = None
        if proxy_env:
            proxy_url = proxy_env.get("HTTP_PROXY") or proxy_env.get("http_proxy")

        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self._call_timeout_s),
            proxy=proxy_url,
            follow_redirects=True,
        )

    # --- lifecycle -----------------------------------------------------------

    async def connect(
        self,
        *,
        proxy_env: dict[str, str] | None = None,
    ) -> ClientSession:
        """Connect to the MCP server over Streamable-HTTP.

        Creates a fresh httpx.AsyncClient (with optional proxy settings), then
        wraps the SDK's `streamable_http_client` in a ClientSession. Times out
        after `init_timeout_s` on the initialize() call. Any failure during
        connect (HTTP error, timeout, transport error) is surfaced as an
        exception so the caller can record status="error".

        Args:
            proxy_env: Optional proxy env dict from sandbox._container.proxy_env()
        """
        self._http_client = self._build_http_client(proxy_env)

        # The modern streamable_http_client API takes an http_client parameter.
        # We pass our pre-configured client so proxy + headers are honored.
        self._transport_ctx = streamable_http_client(
            self._url,
            http_client=self._http_client,
            terminate_on_close=True,
        )

        try:
            read, write, _get_session_id = await self._transport_ctx.__aenter__()

            session = ClientSession(read, write)
            self._session = session
            await session.__aenter__()

            try:
                await asyncio.wait_for(
                    session.initialize(), timeout=self._init_timeout_s
                )
            except TimeoutError as err:
                raise TimeoutError(
                    f"MCP server {self._name!r} at {self._url} did not respond "
                    f"to initialize() within {self._init_timeout_s}s"
                ) from err

            return session
        except Exception:
            # Any failure during connect (HTTP error, anyio task group error,
            # transport error, JSON parse error) — clean up and re-raise.
            # The caller (runtime) catches this and surfaces status="error".
            await self._cleanup_after_failure()
            raise

    async def _cleanup_after_failure(self) -> None:
        """Clean up partial state after a connect failure.

        The streaming client may leave dangling anyio task groups; we
        close what we can and suppress secondary errors."""
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport_ctx is not None:
            with contextlib.suppress(Exception):
                await self._transport_ctx.__aexit__(None, None, None)
            self._transport_ctx = None

    @property
    def session(self) -> ClientSession:
        """The live ClientSession — None before connect()."""
        if self._session is None:
            raise RuntimeError("McpHttpClient not connected")
        return self._session

    @property
    def allowed_hosts(self) -> list[str]:
        """The server's configured allowed_hosts for egress allowlisting."""
        return list(self._allowed_hosts)

    async def close(self) -> None:
        """Drain the session + transport, close the HTTP client."""
        if self._session is not None:
            try:
                await asyncio.wait_for(
                    self._session.__aexit__(None, None, None), timeout=2.0
                )
            except Exception:
                pass
            self._session = None

        if self._transport_ctx is not None:
            try:
                await asyncio.wait_for(
                    self._transport_ctx.__aexit__(None, None, None), timeout=2.0
                )
            except Exception:
                pass
            self._transport_ctx = None

        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

    async def list_tools(self) -> list:
        """Fetch the server's tool list via the MCP session."""
        result = await self.session.list_tools()
        return list(result.tools)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the MCP server and return the raw result dict."""
        result = await self.session.call_tool(tool_name, arguments)
        return {
            "content": list(result.content),
            "isError": getattr(result, "isError", False),
        }

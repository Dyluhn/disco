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
from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
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
            raise ValueError(f"MCP server {server.name!r}: streamable_http requires a url")

        self._name = server.name
        self._url = server.url
        self._headers_config = dict(server.headers or {})
        self._allowed_hosts = list(server.allowed_hosts or [])
        self._secrets = secrets
        self._init_timeout_s = init_timeout_s
        self._call_timeout_s = call_timeout_s

        self._session: ClientSession | None = None
        # The MCP SDK's streamable-HTTP context owns an AnyIO cancel scope.
        # Entering it in an app-startup/request task and exiting it later from a
        # reload/shutdown task raises "Attempted to exit cancel scope in a
        # different task". A single owner task therefore enters *and* exits the
        # HTTP client, transport, and ClientSession contexts. Public callers only
        # signal that owner and may safely connect/close from different tasks.
        self._owner_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # --- helpers ------------------------------------------------------------

    def _resolve_headers(self) -> dict[str, str]:
        """Resolve SecretRefs to plaintext values via the SecretsStore CLOSURE.
        The secret is resolved HERE — never passed to the sandbox."""
        resolved: dict[str, str] = {}
        for key, ref in self._headers_config.items():
            if not secret_ref_allowed_for_origin(ref, self._url):
                _LOG.warning(
                    "McpHttpClient: secret %r is not allowed for %s; dropping header %r",
                    ref,
                    self._url,
                    key,
                )
                continue
            if self._secrets is not None:
                val = self._secrets.get_secret(ref)
                if val is not None:
                    resolved[key] = val
                else:
                    _LOG.debug("McpHttpClient: secret %r for header %r not found", ref, key)
            else:
                _LOG.debug("McpHttpClient: no secrets store; skipping header %r", key)
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
            follow_redirects=False,
            trust_env=False,
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
        if self._session is not None:
            return self._session
        if self._owner_task is not None and not self._owner_task.done():
            raise RuntimeError(f"MCP server {self._name!r} connection is already starting")

        loop = asyncio.get_running_loop()
        ready: asyncio.Future[ClientSession] = loop.create_future()
        self._stop_event = asyncio.Event()
        self._owner_task = asyncio.create_task(
            self._run_connection(proxy_env, ready, self._stop_event),
            name=f"mcp-http:{self._name}",
        )
        try:
            return await ready
        except BaseException:
            # _run_connection owns every context and performs the teardown.
            # Await it so a failed initialize cannot leave a dangling task.
            owner = self._owner_task
            if owner is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await owner
            raise

    async def _run_connection(
        self,
        proxy_env: dict[str, str] | None,
        ready: asyncio.Future[ClientSession],
        stop: asyncio.Event,
    ) -> None:
        """Own the complete SDK lifecycle in one asyncio task."""

        try:
            async with self._build_http_client(proxy_env) as http_client:
                async with streamable_http_client(
                    self._url,
                    http_client=http_client,
                    terminate_on_close=True,
                ) as (read, write, _get_session_id):
                    async with ClientSession(read, write) as session:
                        try:
                            await asyncio.wait_for(
                                session.initialize(), timeout=self._init_timeout_s
                            )
                        except TimeoutError as err:
                            raise TimeoutError(
                                f"MCP server {self._name!r} did not respond to "
                                f"initialize() within {self._init_timeout_s}s"
                            ) from err
                        self._session = session
                        if not ready.done():
                            ready.set_result(session)
                        await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                _LOG.warning(
                    "McpHttpClient: connection owner for %r stopped: %s",
                    self._name,
                    type(exc).__name__,
                )
        finally:
            self._session = None

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
        """Ask the lifecycle owner to drain all contexts, from any caller task."""
        owner = self._owner_task
        stop = self._stop_event
        if owner is None:
            self._session = None
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=3.0)
        except TimeoutError:
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await owner
        finally:
            self._session = None
            self._owner_task = None
            self._stop_event = None

    async def list_tools(self) -> list:
        """Fetch the server's tool list via the MCP session."""
        result = await self.session.list_tools()
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the MCP server and return the raw result dict."""
        result = await self.session.call_tool(tool_name, arguments)
        return {
            "content": list(result.content),
            "isError": getattr(result, "isError", False),
        }

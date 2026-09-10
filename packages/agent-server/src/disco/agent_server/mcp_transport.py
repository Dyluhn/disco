"""MCP streamable_http transport lifecycle + egress/proxy posture.

Split out of `mcp_manager.py` (pure move, zero behavior change) to bring
`McpManager` back under its class-size budget. Two cohesive pieces live here:

- `McpHttpConnector` owns the HTTP transport's retry ladder, the
  origin/secret-ref approval gate, and ToolDef construction for
  `streamable_http` servers — everything `_start_http_server`/`_connect_http`
  used to do on `McpManager` directly. `McpManager` keeps a `_http`
  collaborator attribute plus thin private properties (`_http_clients`,
  `_http_tools`, `_http_status`) delegating to `.clients` / `.tools` /
  `.status` here, so external readers (`routes/mcp.py`, the test suite) that
  reach into `runtime.mcp._http_clients` etc. keep working unchanged. The
  connector never references the retry attempt through `McpManager` — the
  manager passes its own (monkeypatchable) `_connect_http` bound method in as
  the `connect` callable, so tests that do
  ``manager._connect_http = AsyncMock(...)`` still intercept the real retry
  loop.
- `compute_mcp_egress_hosts` / `compute_mcp_proxy_env` are pure computations
  over an HTTP-client map + env, previously `_mcp_egress_hosts` /
  `_mcp_proxy_env` methods on `McpManager`. `McpManager` keeps same-named
  thin delegator methods (production code and tests call them as
  `runtime.mcp._mcp_egress_hosts()` / `..._mcp_proxy_env(url)`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from disco.core.env import disco_env
from disco.core.llm import ConfigStore, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools import ToolDef
from disco.tools.mcp import McpServerConfig
from disco.tools.mcp.http import McpHttpClient

_LOG = logging.getLogger(__name__)

# Connection recovery is deliberately small and deterministic: one immediate
# attempt plus two bounded retries. App startup runs this work in a background
# task, so a black-holed MCP endpoint never owns readiness. Settings reload may
# await the same sequence and receives an honest degraded result within a fixed
# upper bound (3 * init timeout + the declared backoff).
_HTTP_CONNECT_ATTEMPTS = 3
_HTTP_INIT_TIMEOUT_S = 3.0
_HTTP_RETRY_DELAYS_S = (0.25, 1.0)


def _diagnostic_exception_type(exc: BaseException) -> str:
    """Return a useful leaf type without exposing exception messages."""

    leaves: list[BaseException] = []

    def collect(current: BaseException) -> None:
        if isinstance(current, BaseExceptionGroup):
            for nested in current.exceptions:
                collect(nested)
            return
        leaves.append(current)

    collect(exc)
    if not leaves:
        return type(exc).__name__
    priority = {
        "ConnectError": 0,
        "ConnectTimeout": 1,
        "ConnectionError": 2,
        "TimeoutError": 3,
        "ReadTimeout": 4,
        "WriteTimeout": 5,
        "PoolTimeout": 6,
    }
    return type(
        min(
            enumerate(leaves),
            key=lambda indexed: (priority.get(type(indexed[1]).__name__, 100), indexed[0]),
        )[1]
    ).__name__


def compute_mcp_egress_hosts(http_clients: dict[str, McpHttpClient]) -> frozenset[str]:
    """Compute the UNION of MCP HTTP server hosts for egress allowlisting.

    Returns hosts from enabled HTTP servers' URLs + allowed_hosts config.
    Empty frozenset if no HTTP servers are configured or started.
    """
    from disco.tools.mcp.http_egress import build_egress_union

    url_hosts: list[str] = []
    allowed_hosts: list[str] = []

    for client in http_clients.values():
        url_hosts.append(client._url)
        allowed_hosts.extend(client.allowed_hosts)

    if not url_hosts and not allowed_hosts:
        return frozenset()

    return build_egress_union(
        frozenset(),
        http_server_urls=url_hosts,
        http_server_allowed_hosts=allowed_hosts,
    )


def _mcp_egress_proxy_required() -> bool:
    """True when the operator runs their OWN orchestrator-side egress sidecar and
    wants approved MCP origins routed through it anyway (opt-in, default off)."""

    raw = disco_env("MCP_EGRESS_PROXY_REQUIRED", "0")
    assert raw is not None  # default above is non-None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def compute_mcp_proxy_env(
    url: str | None,
    http_clients: dict[str, McpHttpClient],
    *,
    origin_approved: bool = False,
) -> dict[str, str] | None:
    """The HTTP(S)_PROXY env the orchestrator-side MCP HTTP client routes
    through, governed by the SAME PMX_BUILD_EGRESS posture as the sandbox spec
    (single source of truth — no divergent egress policy).

    filtered → proxy_env(host, EGRESS_PROXY_PORT); a host outside the unioned
    allowlist is denied 403 by the proxy, so the client cannot bypass the
    sidecar (workorder §2 "no path bypasses it"). BP-G10: filtered is now
    the default (matches the new default sandbox posture). open (explicit
    PMX_BUILD_EGRESS=open) → None (direct). host comes from
    PMX_MCP_EGRESS_PROXY_HOST (default loopback). See
    current/docs/workorders/RP-05b-orchestrator-proxy-decision.md.

    `origin_approved` is the operator-approval bypass. The shipped Compose
    stack runs NO egress sidecar (the proxy is created per-sandbox, with a
    per-run allowlist, by the sandbox backends — there is no long-lived
    orchestrator-side instance and a static one could not track an allowlist
    that changes whenever Settings changes). Under the default `filtered`
    posture that made every remote MCP server unreachable: nothing listens on
    127.0.0.1:8888 inside the agent-server container, so the httpx client got
    ConnectionRefused. The proxy's only job here is to enforce an allowlist
    that is itself DERIVED from the configured MCP servers
    (`compute_mcp_egress_hosts`), and a server only reaches this point after
    passing the config-hash approval plus `origin_approved` — the same
    operator decision, made earlier and persisted. So an approved origin
    connects directly; anything not approved never gets a client at all
    (`McpHttpConnector.connect` raises before this is called). Redirect
    laundering is not a hole: `McpHttpClient` sets follow_redirects=False.
    Operators who DO run their own outbound sidecar can force every MCP
    connection back through it with DISCO_MCP_EGRESS_PROXY_REQUIRED=1.
    """
    # `http_clients` is accepted (not read) to keep a uniform (url, http_clients)
    # call convention with compute_mcp_egress_hosts; this posture is env-only.
    egress_posture = disco_env("BUILD_EGRESS", "filtered")
    assert egress_posture is not None  # default above is non-None
    posture = egress_posture.lower().strip()
    if posture != "filtered":
        return None
    # An approved loopback MCP server is already confined to the Agent
    # host. Sending it through an optional outbound sidecar makes local
    # Settings connections fail whenever that sidecar is absent. Bypass is
    # deliberately limited to URL-parsed loopback names/addresses; remote,
    # unspecified, link-local, and private-network hosts remain proxied.
    host_name = (urlsplit(url).hostname or "").lower().rstrip(".") if url else ""
    if host_name == "localhost":
        return None
    try:
        if host_name and ipaddress.ip_address(host_name).is_loopback:
            return None
    except ValueError:
        pass
    if origin_approved and not _mcp_egress_proxy_required():
        return None
    from disco.tools.sandbox._container import EGRESS_PROXY_PORT, proxy_env

    host = disco_env("MCP_EGRESS_PROXY_HOST", "127.0.0.1")
    assert host is not None  # default above is non-None
    return proxy_env(host, EGRESS_PROXY_PORT)


class McpHttpConnector:
    """Own the MCP streamable_http transport: connect/retry lifecycle, the
    origin + secret-ref approval gate, ToolDef construction, and per-server
    status. Constructed once by `McpManager` and reached through `self._http`.
    """

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        config_store: ConfigStore,
        store: SqliteEventStore,
        approval_pending: dict[str, dict[str, object]],
    ) -> None:
        self._secret_store = secret_store
        self._config_store = config_store
        self._store = store
        self._approval_pending = approval_pending
        self.clients: dict[str, McpHttpClient] = {}
        self.tools: dict[str, ToolDef] = {}
        self.status: dict[str, dict[str, Any]] = {}

    def set_status(
        self,
        name: str,
        status: str,
        *,
        code: str | None = None,
        attempts: int | None = None,
        exception_type: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {"status": status}
        if code is not None:
            diagnostic: dict[str, Any] = {"code": code}
            if attempts is not None:
                diagnostic["attempts"] = attempts
            if exception_type is not None:
                diagnostic["exception_type"] = exception_type
            entry["diagnostic"] = diagnostic
        self.status[name] = entry

    async def start_server(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
        config_approvals: dict[str, str],
        *,
        connect: Callable[[str, McpServerConfig, dict[str, str]], Awaitable[None]],
    ) -> None:
        from disco.tools.mcp.approval import (
            ApprovalRequired,
            ConfigApprovalRequired,
            compute_config_hash,
        )

        if not srv.enabled:
            self.set_status(name, "disabled")
            return

        for attempt in range(1, _HTTP_CONNECT_ATTEMPTS + 1):
            self.set_status(name, "connecting", attempts=attempt)
            try:
                current_config_hash = compute_config_hash(srv)
                stored_config_hash = config_approvals.get(name)
                if stored_config_hash != current_config_hash:
                    raise ConfigApprovalRequired(
                        name, stored_config_hash or "", current_config_hash
                    )
                await connect(name, srv, approvals)
                self.set_status(name, "connected", attempts=attempt)
                return
            except ConfigApprovalRequired as exc:
                self._approval_pending[name] = {
                    "kind": "config",
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                self.set_status(name, "approval_required")
                return
            except ApprovalRequired as exc:
                self._approval_pending[name] = {
                    "kind": "tools",
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                http_db_conn = getattr(self._store, "_conn", None)
                if http_db_conn is not None:
                    try:
                        from disco.tools.mcp.migrations import (
                            set_mcp_approval_pending,
                        )

                        set_mcp_approval_pending(
                            http_db_conn,
                            name,
                            exc.old_hash,
                            exc.new_hash,
                        )
                    except Exception:
                        _LOG.warning(
                            "MCP HTTP approval drift persistence failed for %r",
                            name,
                            exc_info=True,
                        )
                await self.discard_client(name)
                self.set_status(name, "approval_required")
                return
            except Exception as exc:
                await self.discard_client(name)
                diagnostic_type = _diagnostic_exception_type(exc)
                if attempt < _HTTP_CONNECT_ATTEMPTS:
                    delay = _HTTP_RETRY_DELAYS_S[attempt - 1]
                    _LOG.warning(
                        "MCP HTTP server %r connection attempt %d/%d failed (%s); "
                        "retrying in %.2fs",
                        name,
                        attempt,
                        _HTTP_CONNECT_ATTEMPTS,
                        diagnostic_type,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                self.set_status(
                    name,
                    "degraded",
                    code="mcp_connection_failed",
                    attempts=attempt,
                    exception_type=diagnostic_type,
                )
                _LOG.warning(
                    "MCP HTTP server %r degraded after %d bounded attempts (%s)",
                    name,
                    attempt,
                    diagnostic_type,
                )

    async def discard_client(self, name: str) -> None:
        client = self.clients.pop(name, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def connect(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
    ) -> None:
        """Connect one HTTP MCP server, build ToolDefs, verify approval hash."""
        from disco.tools.behavior import OPAQUE_MCP_BEHAVIOR
        from disco.tools.mcp.approval import (
            ApprovalRequired,
            compute_description_hash,
        )
        from disco.tools.mcp.http import McpHttpClient
        from disco.tools.mcp.naming import qualified_name
        from disco.tools.mcp.pool import _UNTRUSTED_DESC_WRAPPER, _schema_to_args_model

        proxy = self._gate_and_proxy_env(name, srv)
        client = McpHttpClient(
            server=srv,
            secrets=self._secret_store,
            init_timeout_s=_HTTP_INIT_TIMEOUT_S,
            call_timeout_s=10.0,
        )

        try:
            await client.connect(proxy_env=proxy)
        except Exception:
            self.clients[name] = client  # register for close
            raise

        self.clients[name] = client

        # List tools and build ToolDefs
        raw_tools = await client.list_tools()

        # Compute the description hash and check approval
        tool_descs = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
            }
            for t in raw_tools
        ]
        new_hash = compute_description_hash(tool_descs)

        stored = approvals.get(name)
        if stored != new_hash:
            raise ApprovalRequired(name, stored or "", new_hash)

        # Build ToolDefs
        allowed = set(srv.allowed_tools) if srv.allowed_tools is not None else None

        for tool in raw_tools:
            if allowed is not None and tool.name not in allowed:
                _LOG.debug("McpPool: HTTP tool %r not in allowlist for %r", tool.name, name)
                continue

            qname = qualified_name(name, tool.name)
            if qname in self.tools:
                _LOG.warning("McpPool: HTTP tool %r already registered — skipping", qname)
                continue

            fenced_desc = _UNTRUSTED_DESC_WRAPPER.format(
                name=name, desc=tool.description or "(no description)"
            )

            self.tools[qname] = ToolDef(
                name=qname,
                description=fenced_desc,
                args_model=_schema_to_args_model(tool),
                needs=frozenset(),
                base_risk=srv.risk_tier,
                runs_in="in_process",  # HTTP always runs in_process (orchestrator-side)
                read_only=False,
                uses_capabilities=frozenset(),
                behavior=OPAQUE_MCP_BEHAVIOR,
            )

        _LOG.info(
            "McpPool: HTTP server %r connected — %d tool(s) registered",
            name,
            len(raw_tools),
        )

    def _gate_and_proxy_env(self, name: str, srv: McpServerConfig) -> dict[str, str] | None:
        """Apply the operator gate, then decide this connection's egress route.

        ONE evaluation of `origin_approved` serves both: it refuses an
        unapproved origin (no client is ever constructed for one) and it is the
        `origin_approved` flag `compute_mcp_proxy_env` bypasses the — absent —
        egress sidecar on. Keeping them in one place is the point: the bypass
        cannot drift away from the check that admits the connection.

        RP-05b §2: under the `filtered` posture a NON-approved caller still gets
        the proxy env, so an off-allowlist host is denied 403 rather than dialed
        directly. `open` posture → None (direct). See
        current/docs/workorders/RP-05b-orchestrator-proxy-decision.md.
        """
        approved = bool(srv.url) and self.origin_approved(name, srv)
        if srv.url and not approved:
            raise RuntimeError("MCP HTTP origin is not operator-approved")
        return compute_mcp_proxy_env(srv.url, self.clients, origin_approved=approved)

    def origin_approved(self, name: str, srv: McpServerConfig) -> bool:
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        if not srv.url:
            return False
        refs = self.secret_refs(srv)
        return all(
            self._config_store.approvals.origin_approved(
                srv.url,
                f"mcp:{name}",
                ref,
                secret_store=self._secret_store,
            )
            and secret_ref_allowed_for_origin(ref, srv.url)
            for ref in refs
        )

    def secret_refs(self, srv: McpServerConfig) -> tuple[str, ...]:
        refs = tuple(
            sorted(str(v).strip() for v in (srv.headers or {}).values() if str(v).strip())
        )
        return refs or ("",)

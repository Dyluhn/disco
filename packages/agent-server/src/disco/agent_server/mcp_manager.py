"""MCP client-pool + retrieval-tier lifecycle — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The MCP wiring
(stdio pool start/stop, streamable_http clients, retrieval-tier provider
composition, egress/proxy posture, approval-drift state) is collected into a
single `McpManager` collaborator constructed once in `ConversationRuntime`.

The MCP STATE attributes (`_mcp_pool`, `_mcp_http_clients`, `_mcp_http_tools`,
`_mcp_retrieval_searches`, `_mcp_retrieval_extractions`, `_mcp_approval_pending`)
and the shared `_cap_handlers` cache remain declared on `ConversationRuntime`
(the composition root) — the agent-server test-suite reads/writes several of
them directly on the runtime (`rt._mcp_pool`, `rt._mcp_http_clients`,
`rt._mcp_approval_pending`) and `_compose_build_loop` (which stays on the
runtime) reads `self._mcp_pool` / `self._mcp_call_target`. So `McpManager`
reaches that shared state through a back-reference (`self._rt`), and the
runtime keeps thin one-line delegators so every external call site is
unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

from disco.core.env import disco_env
from disco.core.llm import ConfigStore, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools import ToolDef
from disco.tools.mcp import McpPool, McpServerConfig
from disco.tools.mcp.http import McpHttpClient
from pydantic import ValidationError

from .build_loop_components import (
    McpApprovalNotice,
    McpCallTarget,
    McpLoopSnapshot,
)

_LOG = logging.getLogger(__name__)

# Connection recovery is deliberately small and deterministic: one immediate
# attempt plus two bounded retries. App startup runs this work in a background
# task, so a black-holed MCP endpoint never owns readiness. Settings reload may
# await the same sequence and receives an honest degraded result within a fixed
# upper bound (3 * init timeout + the declared backoff).
_HTTP_CONNECT_ATTEMPTS = 3
_HTTP_INIT_TIMEOUT_S = 3.0
_HTTP_RETRY_DELAYS_S = (0.25, 1.0)


class _MergedCallTarget:
    """Prefer HTTP transport, then fall back to the stdio pool."""

    def __init__(
        self,
        pool: McpPool | None,
        http_client_for: Callable[[str], McpHttpClient | None],
    ) -> None:
        self._pool = pool
        self._http_client_for = http_client_for

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        client = self._http_client_for(server)
        if client is not None:
            return cast(dict[str, object], await client.call_tool(tool, arguments))
        if self._pool is not None:
            return cast(
                dict[str, object],
                await self._pool.call_tool(server, tool, arguments),
            )
        raise RuntimeError(f"MCP server {server!r} is not connected")


def _snapshot_for_manager(
    manager: McpManager,
    call_target: McpCallTarget,
    egress_hosts: frozenset[str],
) -> McpLoopSnapshot:
    tools: list[ToolDef] = []
    if manager._pool is not None and manager._pool.started:
        tools.extend(manager._pool.snapshot())
    tools.extend(manager._http_tools.values())
    config = manager._config_store.load()
    approvals = tuple(
        McpApprovalNotice(
            server=server,
            description_hash=str(info.get("new_hash", "")),
            old_description_hash=str(info.get("old_hash", "")),
        )
        for server, info in sorted(manager._approval_pending.items())
    )
    return McpLoopSnapshot(
        tools=tuple(tools),
        max_active_schemas=config.mcp.max_active_schemas if config.mcp else 20,
        call_target=call_target,
        egress_hosts=egress_hosts,
        approvals=approvals,
    )


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


class McpManager:
    """Own the MCP lifecycle, transport caches, and approval state."""

    def __init__(
        self,
        config_store: ConfigStore,
        secret_store: SecretStore,
        store: SqliteEventStore,
    ) -> None:
        self._config_store = config_store
        self._secret_store = secret_store
        self._store = store
        self._reload_lock = asyncio.Lock()
        self._pool: McpPool | None = None
        self._approval_pending: dict[str, dict[str, object]] = {}
        self._http_clients: dict[str, McpHttpClient] = {}
        self._http_tools: dict[str, ToolDef] = {}
        self._http_status: dict[str, dict[str, Any]] = {}
        self._retrieval_searches: list[Any] = []
        self._retrieval_extractions: list[Any] = []

    def _forget(self, conversation_id: str) -> None:
        self._approval_pending.pop(conversation_id, None)

    def _set_http_status(
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
        self._http_status[name] = entry

    @staticmethod
    def _typed_server(name: str, raw: object) -> McpServerConfig:
        """Normalize loose/typed persisted input at one redacted boundary."""

        from disco.tools.mcp.config import McpServerConfig as TypedMcpServerConfig

        payload = raw.model_dump() if isinstance(raw, TypedMcpServerConfig) else raw
        if not isinstance(payload, dict):
            raise TypeError("MCP server config must be an object")
        return TypedMcpServerConfig.model_validate({**payload, "name": name})

    async def reload(self) -> dict[str, Any]:
        """Atomically make persisted MCP config truthful for subsequent turns."""

        async with self._reload_lock:
            await self._close_mcp_pool()
            self._approval_pending.clear()
            await self._start_mcp_pool()
            servers = self._config_store.load().mcp.servers
            pool_status = (
                self._pool.server_status() if self._pool is not None else {}
            )
            pool_tools = (
                [tool.name for tool in self._pool.snapshot()]
                if self._pool is not None
                else []
            )
            connected = sorted(
                {
                    *(
                        name
                        for name, state in self._http_status.items()
                        if state.get("status") == "connected"
                    ),
                    *(name for name, status in pool_status.items() if status == "connected"),
                }
            )
            server_statuses = {
                name: dict(state) for name, state in self._http_status.items()
            }
            server_statuses.update(
                {name: {"status": status} for name, status in pool_status.items()}
            )
            return {
                "ok": True,
                "configured_servers": sorted(servers),
                "connected_servers": connected,
                "registered_tools": sorted(
                    {
                        *self._http_tools,
                        *pool_tools,
                    }
                ),
                "approval_required": sorted(self._approval_pending),
                "server_statuses": server_statuses,
            }

    def _compose_mcp_retrieval(self, deps: dict[str, Any]) -> tuple[Any, Any]:
        """Compose the bundled search/extraction providers with the conversation's
        MCP retrieval providers (RP-05b §3) so MCP-discovered URLs flow through the
        SAME GroundingPipeline as bundled hits — reranked, extracted, NLI-verified,
        cited identically. Inert (returns the primaries unchanged) when no MCP
        retrieval-shaped tools are configured. THIS is the join that makes the
        retrieval tier reach the pipeline — registering providers under unconsumed
        broker names did not."""
        from disco.tools.mcp.retrieval_tier import compose_with_mcp

        return compose_with_mcp(
            deps["search"],
            deps["extraction"],
            mcp_searches=self._retrieval_searches,
            mcp_extractions=self._retrieval_extractions,
        )

    def _mcp_egress_hosts(self) -> frozenset[str]:
        """Compute the UNION of MCP HTTP server hosts for egress allowlisting.

        Returns hosts from enabled HTTP servers' URLs + allowed_hosts config.
        Empty frozenset if no HTTP servers are configured or started.
        """
        from disco.tools.mcp.http_egress import build_egress_union

        url_hosts: list[str] = []
        allowed_hosts: list[str] = []

        for client in self._http_clients.values():
            url_hosts.append(client._url)
            allowed_hosts.extend(client.allowed_hosts)

        if not url_hosts and not allowed_hosts:
            return frozenset()

        return build_egress_union(
            frozenset(),
            http_server_urls=url_hosts,
            http_server_allowed_hosts=allowed_hosts,
        )

    def _mcp_proxy_env(self, url: str | None = None) -> dict[str, str] | None:
        """The HTTP(S)_PROXY env the orchestrator-side MCP HTTP client routes
        through, governed by the SAME PMX_BUILD_EGRESS posture as the sandbox spec
        (single source of truth — no divergent egress policy).

        filtered → proxy_env(host, EGRESS_PROXY_PORT); a host outside the unioned
        allowlist is denied 403 by the proxy, so the client cannot bypass the
        sidecar (workorder §2 "no path bypasses it"). BP-G10: filtered is now
        the default (matches the new default sandbox posture). open (explicit
        PMX_BUILD_EGRESS=open) → None (direct). host comes from
        PMX_MCP_EGRESS_PROXY_HOST (default loopback). See
        docs/workorders/RP-05b-orchestrator-proxy-decision.md."""
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
        from disco.tools.sandbox._container import EGRESS_PROXY_PORT, proxy_env

        host = disco_env("MCP_EGRESS_PROXY_HOST", "127.0.0.1")
        assert host is not None  # default above is non-None
        return proxy_env(host, EGRESS_PROXY_PORT)

    async def _start_mcp_pool(self) -> None:
        """Start configured stdio and streamable-HTTP clients with approvals."""
        cfg = self._config_store.load()
        mcp_cfg = cfg.mcp
        self._http_status.clear()
        if not mcp_cfg.enabled or not mcp_cfg.servers:
            if not mcp_cfg.enabled:
                for name in mcp_cfg.servers:
                    self._set_http_status(name, "disabled")
            return

        from disco.tools.mcp.config import (
            McpSettings as TypedMcpSettings,
        )
        from disco.tools.mcp.migrations import (
            list_mcp_approvals,
            list_mcp_config_approvals,
        )

        # Read existing approvals from the DB (D1: security gate production path)
        approvals: dict[str, str] = {}
        config_approvals: dict[str, str] = {}
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                for row in list_mcp_approvals(conn):
                    approvals[row["server"]] = row["description_hash"]
                for row in list_mcp_config_approvals(conn):
                    config_approvals[row["server"]] = row["config_hash"]
        except Exception:
            _LOG.warning("MCP pool: failed to read approvals from DB", exc_info=True)

        # Split servers: stdio → pool, streamable_http → HTTP clients. The
        # core RouterConfig.mcp.servers schema is `dict[str, dict]` (loose,
        # for Settings writeback), so the value type isn't McpServerConfig
        # out of the loader — upgrade each entry to the typed model so the
        # transport/risk-tier fields are real attributes the pool/HTTP
        # branch can branch on. The Pydantic coerce also surfaces any
        # misconfiguration as a clear ValidationError at startup.
        http_servers: dict[str, McpServerConfig] = {}
        stdio_servers: dict[str, McpServerConfig] = {}
        for name, srv_raw in mcp_cfg.servers.items():
            try:
                srv = self._typed_server(name, srv_raw)
            except (TypeError, ValidationError) as exc:
                # One malformed persisted row is a per-server diagnostic, never
                # a server-wide startup failure. Do not log validation input:
                # it may contain header/env secret references or commands.
                self._set_http_status(
                    name,
                    "error",
                    code="invalid_mcp_server_config",
                    exception_type=type(exc).__name__,
                )
                _LOG.warning(
                    "MCP server %r has invalid persisted configuration (%s)",
                    name,
                    type(exc).__name__,
                )
                continue
            if srv.transport == "streamable_http":
                http_servers[name] = srv
            else:
                stdio_servers[name] = srv

        # Start stdio pool
        if stdio_servers:
            typed = TypedMcpSettings(
                enabled=mcp_cfg.enabled,
                servers=stdio_servers,
                max_active_schemas=mcp_cfg.max_active_schemas,
            )
            self._pool = McpPool(
                typed,
                secrets=self._secret_store,
                approvals=approvals,
                config_approvals=config_approvals,
            )
            try:
                await self._pool.start()
            except Exception:
                _LOG.warning("MCP pool: failed to start", exc_info=True)

            # D1/D3: collect servers that need re-approval from the pool status
            if self._pool is not None:
                # E6 (#10): the pool is the source of the AUTHORITATIVE new_hash
                # (the SHA-256 of the canonicalized tool descriptions the live
                # server just advertised). Persist it to the shared
                # mcp_approval_pending table so the app-server — which serves
                # GET /api/mcp to the frontend — can surface it on the
                # ApprovalDiff. Without this row, the UI only sees the STORED
                # (old) hash from mcp_approvals, which makes the diff useless
                # (or worse, shows the same value for both old and new).
                pending_db_conn = getattr(self._store, "_conn", None)
                for name, info in self._pool.approval_pending().items():
                    kind = info.get("kind", "tools")
                    old_hash = info.get("old_hash", "")
                    new_hash = info.get("new_hash", "")
                    self._approval_pending[name] = {
                        "kind": kind,
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                    }
                    _LOG.warning(
                        "MCP pool: server %r refused — re-approval required (old=%s… new=%s…)",
                        name,
                        old_hash[:12],
                        new_hash[:12],
                    )
                    if kind == "tools" and pending_db_conn is not None and new_hash:
                        try:
                            from disco.tools.mcp.migrations import (
                                set_mcp_approval_pending,
                            )

                            set_mcp_approval_pending(
                                pending_db_conn,
                                name,
                                old_hash,
                                new_hash,
                            )
                        except Exception:
                            _LOG.warning(
                                "MCP pool: failed to persist pending approval "
                                "for %r (UI will fall back to stored hash only)",
                                name,
                                exc_info=True,
                            )

        # Start HTTP servers concurrently. One black-holed endpoint therefore
        # cannot delay a healthy sibling, and each server owns a small bounded
        # retry ladder rather than an unbounded global reconnect loop.
        if http_servers:
            await asyncio.gather(
                *(
                    self._start_http_server(name, srv, approvals, config_approvals)
                    for name, srv in http_servers.items()
                )
            )

        # Build retrieval-tier MCP providers from the tool list
        await self._build_mcp_retrieval_providers()

    async def _start_http_server(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
        config_approvals: dict[str, str],
    ) -> None:
        from disco.tools.mcp.approval import (
            ApprovalRequired,
            ConfigApprovalRequired,
            compute_config_hash,
        )

        if not srv.enabled:
            self._set_http_status(name, "disabled")
            return

        for attempt in range(1, _HTTP_CONNECT_ATTEMPTS + 1):
            self._set_http_status(name, "connecting", attempts=attempt)
            try:
                current_config_hash = compute_config_hash(srv)
                stored_config_hash = config_approvals.get(name)
                if stored_config_hash != current_config_hash:
                    raise ConfigApprovalRequired(
                        name, stored_config_hash or "", current_config_hash
                    )
                await self._connect_http(name, srv, approvals)
                self._set_http_status(name, "connected", attempts=attempt)
                return
            except ConfigApprovalRequired as exc:
                self._approval_pending[name] = {
                    "kind": "config",
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                self._set_http_status(name, "approval_required")
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
                await self._discard_http_client(name)
                self._set_http_status(name, "approval_required")
                return
            except Exception as exc:
                await self._discard_http_client(name)
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
                self._set_http_status(
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

    async def _discard_http_client(self, name: str) -> None:
        client = self._http_clients.pop(name, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def _connect_http(
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

        if srv.url and not self._mcp_origin_approved(name, srv):
            raise RuntimeError("MCP HTTP origin is not operator-approved")
        client = McpHttpClient(
            server=srv,
            secrets=self._secret_store,
            init_timeout_s=_HTTP_INIT_TIMEOUT_S,
            call_timeout_s=10.0,
        )

        # RP-05b §2: route the orchestrator-side MCP client's outbound httpx
        # through the egress allowlisting proxy when the build posture is
        # filtered, so a host outside the unioned allowlist is denied 403 — the
        # client must NOT bypass the sidecar. open posture → None (direct). See
        # docs/workorders/RP-05b-orchestrator-proxy-decision.md.
        try:
            await client.connect(proxy_env=self._mcp_proxy_env(srv.url))
        except Exception:
            self._http_clients[name] = client  # register for close
            raise

        self._http_clients[name] = client

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
            if qname in self._http_tools:
                _LOG.warning("McpPool: HTTP tool %r already registered — skipping", qname)
                continue

            fenced_desc = _UNTRUSTED_DESC_WRAPPER.format(
                name=name, desc=tool.description or "(no description)"
            )

            self._http_tools[qname] = ToolDef(
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

    def _mcp_origin_approved(self, name: str, srv: McpServerConfig) -> bool:
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        if not srv.url:
            return False
        refs = self._mcp_secret_refs(srv)
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

    def _mcp_secret_refs(self, srv: McpServerConfig) -> tuple[str, ...]:
        refs = tuple(sorted(str(v).strip() for v in (srv.headers or {}).values() if str(v).strip()))
        return refs or ("",)

    @property
    def _mcp_call_target(self) -> McpCallTarget:
        """A callable that routes MCP tool invocations to the right transport.

        Stdio tools go through the pool; HTTP tools go through _mcp_http_clients.
        This is used by _MCPToolWrapper at invocation time.
        """
        return _MergedCallTarget(self._pool, self._http_clients.get)

    def _loop_snapshot(self) -> McpLoopSnapshot:
        return _snapshot_for_manager(
            self,
            self._mcp_call_target,
            self._mcp_egress_hosts(),
        )

    def workflow_tool_definitions(self) -> tuple[ToolDef, ...]:
        """Read-only workflow-authoring projection of currently connected tools."""

        pooled = self._pool.snapshot() if self._pool is not None else ()
        return (*pooled, *self._http_tools.values())

    async def _build_mcp_retrieval_providers(self) -> None:
        """Build retrieval-tier MCP providers from registered MCP tools.

        Scans both the stdio pool snapshot and HTTP tools for search/fetch-shaped
        tools and wraps them as SearchProvider / ExtractionProvider Protocol
        instances. These are registered into the broker at compose time.
        """
        from disco.tools.mcp.retrieval_tier import build_retrieval_providers

        # Collect tools from both transports
        mcp_entries: list[dict[str, Any]] = []

        # Stdio tools from the pool
        if self._pool is not None and self._pool.started:
            for tdef in self._pool.snapshot():
                from disco.tools.mcp.naming import split_qualified_name

                parts = split_qualified_name(tdef.name)
                if parts is None:
                    continue
                server, tool_name = parts
                mcp_entries.append(
                    {
                        "server": server,
                        "tool_name": tool_name,
                        "tool": tdef,
                    }
                )

        # HTTP tools
        for qname, tdef in self._http_tools.items():
            from disco.tools.mcp.naming import split_qualified_name

            parts = split_qualified_name(qname)
            if parts is None:
                continue
            server, tool_name = parts
            mcp_entries.append(
                {
                    "server": server,
                    "tool_name": tool_name,
                    "tool": tdef,
                }
            )

        if mcp_entries:
            self._retrieval_searches, self._retrieval_extractions = (
                build_retrieval_providers(
                    mcp_entries,
                    call_fn=self._mcp_call_target.call_tool,
                )
            )
            _LOG.info(
                "MCP retrieval: built %d search + %d extraction provider(s)",
                len(self._retrieval_searches),
                len(self._retrieval_extractions),
            )
            # Drop any cached Build cap-handlers so the next build composes over
            # the freshly-built MCP providers (RP-05b §3).

    async def _close_mcp_pool(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
        for client in list(self._http_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._http_clients.clear()
        self._http_tools.clear()
        self._retrieval_searches.clear()
        self._retrieval_extractions.clear()
        for state in self._http_status.values():
            if state.get("status") in {"connecting", "connected"}:
                state.clear()
                state["status"] = "disconnected"

    def mcp_approval_state(self) -> dict[str, dict]:
        """Return the pending approval state for WS frame dispatch (D3).

        Each key is a server name; value has 'old_hash' and 'new_hash'.
        """
        return dict(self._approval_pending)

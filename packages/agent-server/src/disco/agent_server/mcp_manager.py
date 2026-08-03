"""MCP client-pool + retrieval-tier lifecycle — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The MCP wiring
(stdio pool start/stop, streamable_http clients, retrieval-tier provider
composition, egress/proxy posture, approval-drift state) is collected into a
single `McpManager` collaborator constructed once in `ConversationRuntime`.

The HTTP transport lifecycle (retry ladder, origin/secret-ref approval gate,
ToolDef construction for `streamable_http` servers) and the egress/proxy
posture computations live in `mcp_transport.py` — `McpManager` reaches them
through a `_http` collaborator (`McpHttpConnector`) and thin private
delegators, so every existing call site (`runtime.mcp._http_clients`,
`runtime.mcp._http_status`, `runtime.mcp._mcp_egress_hosts()`, ...) is
unaffected. See `mcp_transport.py`'s module docstring for the split rationale.

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
import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

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
from .mcp_transport import (
    McpHttpConnector,
    compute_mcp_egress_hosts,
    compute_mcp_proxy_env,
)

_LOG = logging.getLogger(__name__)


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


def _mcp_retrieval_entries(tool_defs: Any) -> list[dict[str, Any]]:
    """Project an iterable of qualified `ToolDef`s into retrieval-tier entries
    (`{server, tool_name, tool}`), skipping anything not MCP-qualified."""
    from disco.tools.mcp.naming import split_qualified_name

    entries: list[dict[str, Any]] = []
    for tdef in tool_defs:
        parts = split_qualified_name(tdef.name)
        if parts is None:
            continue
        server, tool_name = parts
        entries.append({"server": server, "tool_name": tool_name, "tool": tdef})
    return entries


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
        self._http = McpHttpConnector(
            secret_store=secret_store,
            config_store=config_store,
            store=store,
            approval_pending=self._approval_pending,
        )
        self._retrieval_searches: list[Any] = []
        self._retrieval_extractions: list[Any] = []

    @property
    def _http_clients(self) -> dict[str, McpHttpClient]:
        return self._http.clients

    @_http_clients.setter
    def _http_clients(self, value: dict[str, McpHttpClient]) -> None:
        self._http.clients = value

    @property
    def _http_tools(self) -> dict[str, ToolDef]:
        return self._http.tools

    @_http_tools.setter
    def _http_tools(self, value: dict[str, ToolDef]) -> None:
        self._http.tools = value

    @property
    def _http_status(self) -> dict[str, dict[str, Any]]:
        return self._http.status

    @_http_status.setter
    def _http_status(self, value: dict[str, dict[str, Any]]) -> None:
        self._http.status = value

    def _forget(self, conversation_id: str) -> None:
        self._approval_pending.pop(conversation_id, None)

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
            pool_status = self._pool.server_status() if self._pool is not None else {}
            pool_tools = (
                [tool.name for tool in self._pool.snapshot()] if self._pool is not None else []
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
            server_statuses = {name: dict(state) for name, state in self._http_status.items()}
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
        """Compute the UNION of MCP HTTP server hosts for egress allowlisting."""
        return compute_mcp_egress_hosts(self._http_clients)

    def _mcp_proxy_env(self, url: str | None = None) -> dict[str, str] | None:
        """The HTTP(S)_PROXY env the orchestrator-side MCP HTTP client routes
        through; see `mcp_transport.compute_mcp_proxy_env` for the policy."""
        return compute_mcp_proxy_env(url, self._http_clients)

    async def _start_mcp_pool(self) -> None:
        """Start configured stdio and streamable-HTTP clients with approvals."""
        cfg = self._config_store.load()
        mcp_cfg = cfg.mcp
        self._http_status.clear()
        if self._mark_all_disabled_if_off(mcp_cfg):
            return

        approvals, config_approvals = self._read_approval_ledger()
        # The core RouterConfig.mcp.servers schema is `dict[str, dict]` (loose,
        # for Settings writeback), not disco.tools.mcp.config.McpSettings (typed,
        # `dict[str, McpServerConfig]`) that McpPool requires — split_typed_servers
        # is the adapter boundary between those two McpSettings types.
        http_servers, stdio_servers = self._split_typed_servers(mcp_cfg.servers)

        await self._start_stdio_pool(stdio_servers, mcp_cfg, approvals, config_approvals)
        await self._start_all_http_servers(http_servers, approvals, config_approvals)

        # Build retrieval-tier MCP providers from the tool list
        await self._build_mcp_retrieval_providers()

    def _mark_all_disabled_if_off(self, mcp_cfg: Any) -> bool:
        """Return True (marking every configured server "disabled" first) when
        MCP is off or nothing is configured — the caller should skip startup."""
        if not mcp_cfg.enabled or not mcp_cfg.servers:
            if not mcp_cfg.enabled:
                for name in mcp_cfg.servers:
                    self._http.set_status(name, "disabled")
            return True
        return False

    def _read_approval_ledger(self) -> tuple[dict[str, str], dict[str, str]]:
        """Read existing approvals from the DB (D1: security gate production path)."""
        from disco.tools.mcp.migrations import (
            list_mcp_approvals,
            list_mcp_config_approvals,
        )

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
        return approvals, config_approvals

    def _split_typed_servers(
        self, servers: Mapping[str, object]
    ) -> tuple[dict[str, McpServerConfig], dict[str, McpServerConfig]]:
        """Split servers: stdio → pool, streamable_http → HTTP clients. Upgrade
        each loose persisted entry to the typed model so the transport/risk-tier
        fields are real attributes the pool/HTTP branch can branch on; the
        Pydantic coerce also surfaces any misconfiguration as a clear
        ValidationError at startup."""
        http_servers: dict[str, McpServerConfig] = {}
        stdio_servers: dict[str, McpServerConfig] = {}
        for name, srv_raw in servers.items():
            try:
                srv = self._typed_server(name, srv_raw)
            except (TypeError, ValidationError) as exc:
                # One malformed persisted row is a per-server diagnostic, never
                # a server-wide startup failure. Do not log validation input:
                # it may contain header/env secret references or commands.
                self._http.set_status(
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
        return http_servers, stdio_servers

    async def _start_stdio_pool(
        self,
        stdio_servers: dict[str, McpServerConfig],
        mcp_cfg: Any,
        approvals: dict[str, str],
        config_approvals: dict[str, str],
    ) -> None:
        if not stdio_servers:
            return

        from disco.tools.mcp.config import (
            McpSettings as TypedMcpSettings,
        )

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

        if self._pool is None:
            return

        # D1/D3: collect servers that need re-approval from the pool status
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

    async def _start_all_http_servers(
        self,
        http_servers: dict[str, McpServerConfig],
        approvals: dict[str, str],
        config_approvals: dict[str, str],
    ) -> None:
        # Start HTTP servers concurrently. One black-holed endpoint therefore
        # cannot delay a healthy sibling, and each server owns a small bounded
        # retry ladder rather than an unbounded global reconnect loop.
        if not http_servers:
            return
        await asyncio.gather(
            *(
                self._start_http_server(name, srv, approvals, config_approvals)
                for name, srv in http_servers.items()
            )
        )

    async def _start_http_server(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
        config_approvals: dict[str, str],
    ) -> None:
        # `connect=self._connect_http` (not `self._http.connect` directly) so a
        # test that does `manager._connect_http = AsyncMock(...)` still
        # intercepts the retry loop the connector runs on our behalf.
        await self._http.start_server(
            name, srv, approvals, config_approvals, connect=self._connect_http
        )

    async def _connect_http(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
    ) -> None:
        await self._http.connect(name, srv, approvals)

    def _mcp_origin_approved(self, name: str, srv: McpServerConfig) -> bool:
        return self._http.origin_approved(name, srv)

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
        if self._pool is not None and self._pool.started:
            mcp_entries.extend(_mcp_retrieval_entries(self._pool.snapshot()))
        mcp_entries.extend(_mcp_retrieval_entries(self._http_tools.values()))

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

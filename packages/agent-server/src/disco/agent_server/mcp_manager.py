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

import contextlib
import logging
from typing import Any

from disco.core.env import disco_env
from disco.tools import ToolDef
from disco.tools.mcp import McpPool, McpServerConfig

_LOG = logging.getLogger(__name__)


class McpManager:
    """Owns the MCP lifecycle logic; shared state lives on the runtime back-ref."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

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
            mcp_searches=self._rt._mcp_retrieval_searches,
            mcp_extractions=self._rt._mcp_retrieval_extractions,
        )

    def _mcp_egress_hosts(self) -> frozenset[str]:
        """Compute the UNION of MCP HTTP server hosts for egress allowlisting.

        Returns hosts from enabled HTTP servers' URLs + allowed_hosts config.
        Empty frozenset if no HTTP servers are configured or started.
        """
        from disco.tools.mcp.http_egress import build_egress_union

        url_hosts: list[str] = []
        allowed_hosts: list[str] = []

        for client in self._rt._mcp_http_clients.values():
            url_hosts.append(client._url)
            allowed_hosts.extend(client.allowed_hosts)

        if not url_hosts and not allowed_hosts:
            return frozenset()

        return build_egress_union(
            frozenset(),
            http_server_urls=url_hosts,
            http_server_allowed_hosts=allowed_hosts,
        )

    def _mcp_proxy_env(self) -> dict[str, str] | None:
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
        from disco.tools.sandbox._container import EGRESS_PROXY_PORT, proxy_env

        host = disco_env("MCP_EGRESS_PROXY_HOST", "127.0.0.1")
        assert host is not None  # default above is non-None
        return proxy_env(host, EGRESS_PROXY_PORT)

    async def _start_mcp_pool(self) -> None:
        """Start the MCP client pool if mcp.enabled + servers are configured.
        Called once at runtime startup (from the app lifespan). Loads approvals
        from the mcp_approvals table. On ApprovalRequired, the affected server
        is refused and the WS frame is dispatched; other servers still start.

        Rung B: also starts streamable_http servers via McpHttpClient."""
        cfg = self._rt._config_store.load()
        mcp_cfg = cfg.mcp
        if not mcp_cfg.enabled or not mcp_cfg.servers:
            return

        from disco.tools.mcp.approval import ApprovalRequired
        from disco.tools.mcp.config import (
            McpServerConfig as TypedMcpServerConfig,
        )
        from disco.tools.mcp.config import (
            McpSettings as TypedMcpSettings,
        )
        from disco.tools.mcp.migrations import list_mcp_approvals

        # Read existing approvals from the DB (D1: security gate production path)
        approvals: dict[str, str] = {}
        try:
            conn = getattr(self._rt._store, "_conn", None)
            if conn is not None:
                for row in list_mcp_approvals(conn):
                    approvals[row["server"]] = row["description_hash"]
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
            srv = TypedMcpServerConfig.model_validate({"name": name, **srv_raw})
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
            self._rt._mcp_pool = McpPool(
                typed, secrets=self._rt._secret_store, approvals=approvals
            )
            try:
                await self._rt._mcp_pool.start()
            except Exception:
                _LOG.warning("MCP pool: failed to start", exc_info=True)

            # D1/D3: collect servers that need re-approval from the pool status
            if self._rt._mcp_pool is not None:
                # E6 (#10): the pool is the source of the AUTHORITATIVE new_hash
                # (the SHA-256 of the canonicalized tool descriptions the live
                # server just advertised). Persist it to the shared
                # mcp_approval_pending table so the app-server — which serves
                # GET /api/mcp to the frontend — can surface it on the
                # ApprovalDiff. Without this row, the UI only sees the STORED
                # (old) hash from mcp_approvals, which makes the diff useless
                # (or worse, shows the same value for both old and new).
                pending_db_conn = getattr(self._rt._store, "_conn", None)
                for name, info in self._rt._mcp_pool.approval_pending().items():
                    old_hash = info.get("old_hash", "")
                    new_hash = info.get("new_hash", "")
                    self._rt._mcp_approval_pending[name] = {
                        "old_hash": old_hash,
                        "new_hash": new_hash,
                    }
                    _LOG.warning(
                        "MCP pool: server %r refused — re-approval required "
                        "(old=%s… new=%s…)",
                        name, old_hash[:12], new_hash[:12],
                    )
                    if pending_db_conn is not None and old_hash and new_hash:
                        try:
                            from disco.tools.mcp.migrations import (
                                set_mcp_approval_pending,
                            )
                            set_mcp_approval_pending(
                                pending_db_conn, name, old_hash, new_hash,
                            )
                        except Exception:
                            _LOG.warning(
                                "MCP pool: failed to persist pending approval "
                                "for %r (UI will fall back to stored hash only)",
                                name,
                                exc_info=True,
                            )

        # Start HTTP servers (rung B)
        for name, srv in http_servers.items():
            if not srv.enabled:
                _LOG.debug("McpPool: HTTP server %r is disabled — skipping", name)
                continue

            try:
                await self._connect_http(name, srv, approvals)
            except ApprovalRequired as exc:
                _LOG.warning(
                    "McpPool: HTTP server %r refused — description_hash changed "
                    "(%s → %s) — re-approval required",
                    name, exc.old_hash[:12], exc.new_hash[:12],
                )
                self._rt._mcp_approval_pending[name] = {
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                # E6: same drift-persistence path as the stdio branch — write
                # the AUTHORITATIVE new_hash the live HTTP server advertised to
                # the shared mcp_approval_pending table so the app-server's
                # GET /api/mcp can surface it on the ApprovalDiff.
                http_db_conn = getattr(self._rt._store, "_conn", None)
                if http_db_conn is not None:
                    try:
                        from disco.tools.mcp.migrations import (
                            set_mcp_approval_pending,
                        )
                        set_mcp_approval_pending(
                            http_db_conn, name, exc.old_hash, exc.new_hash,
                        )
                    except Exception:
                        _LOG.warning(
                            "McpPool: failed to persist pending approval for "
                            "HTTP server %r (UI will fall back to stored hash)",
                            name,
                            exc_info=True,
                        )
                # Clean up the client
                client = self._rt._mcp_http_clients.pop(name, None)
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()
            except Exception as exc:
                _LOG.warning(
                    "McpPool: HTTP server %r failed to start: %s", name, exc
                )
                if name in self._rt._mcp_http_clients:
                    client = self._rt._mcp_http_clients.pop(name)
                    with contextlib.suppress(Exception):
                        await client.close()

        # Build retrieval-tier MCP providers from the tool list
        await self._build_mcp_retrieval_providers()

    async def _connect_http(
        self,
        name: str,
        srv: McpServerConfig,
        approvals: dict[str, str],
    ) -> None:
        """Connect one HTTP MCP server, build ToolDefs, verify approval hash."""
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
            secrets=self._rt._secret_store,
            call_timeout_s=10.0,
        )

        # RP-05b §2: route the orchestrator-side MCP client's outbound httpx
        # through the egress allowlisting proxy when the build posture is
        # filtered, so a host outside the unioned allowlist is denied 403 — the
        # client must NOT bypass the sidecar. open posture → None (direct). See
        # docs/workorders/RP-05b-orchestrator-proxy-decision.md.
        try:
            await client.connect(proxy_env=self._mcp_proxy_env())
        except Exception:
            self._rt._mcp_http_clients[name] = client  # register for close
            raise

        self._rt._mcp_http_clients[name] = client

        # List tools and build ToolDefs
        raw_tools = await client.list_tools()

        # Compute the description hash and check approval
        tool_descs = [
            {"name": t.name, "description": t.description or ""}
            for t in raw_tools
        ]
        new_hash = compute_description_hash(tool_descs)

        stored = approvals.get(name)
        if stored is not None and stored != new_hash:
            raise ApprovalRequired(name, stored, new_hash)

        # Build ToolDefs
        allowed = set(srv.allowed_tools) if srv.allowed_tools is not None else None

        for tool in raw_tools:
            if allowed is not None and tool.name not in allowed:
                _LOG.debug("McpPool: HTTP tool %r not in allowlist for %r", tool.name, name)
                continue

            qname = qualified_name(name, tool.name)
            if qname in self._rt._mcp_http_tools:
                _LOG.warning("McpPool: HTTP tool %r already registered — skipping", qname)
                continue

            fenced_desc = _UNTRUSTED_DESC_WRAPPER.format(
                name=name, desc=tool.description or "(no description)"
            )

            self._rt._mcp_http_tools[qname] = ToolDef(
                name=qname,
                description=fenced_desc,
                args_model=_schema_to_args_model(tool),
                needs=frozenset(),
                base_risk=srv.risk_tier,
                runs_in="in_process",  # HTTP always runs in_process (orchestrator-side)
                read_only=False,
                uses_capabilities=frozenset(),
            )

        _LOG.info(
            "McpPool: HTTP server %r connected — %d tool(s) registered",
            name, len(raw_tools),
        )

    def _mcp_origin_approved(self, name: str, srv: McpServerConfig) -> bool:
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        refs = self._mcp_secret_refs(srv)
        checker = getattr(self._rt, "_origin_approved", None)
        if callable(checker):
            return all(
                checker(srv.url, f"mcp:{name}", ref)
                and secret_ref_allowed_for_origin(ref, srv.url)
                for ref in refs
            )
        return all(
            self._rt._config_store.origin_approved(
                srv.url,
                f"mcp:{name}",
                ref,
                secret_store=self._rt._secret_store,
            )
            and secret_ref_allowed_for_origin(ref, srv.url)
            for ref in refs
        )

    def _mcp_secret_refs(self, srv: McpServerConfig) -> tuple[str, ...]:
        refs = tuple(sorted(str(v).strip() for v in (srv.headers or {}).values() if str(v).strip()))
        return refs or ("",)

    @property
    def _mcp_call_target(self) -> Any:
        """A callable that routes MCP tool invocations to the right transport.

        Stdio tools go through the pool; HTTP tools go through _mcp_http_clients.
        This is used by _MCPToolWrapper at invocation time.
        """
        pool = self._rt._mcp_pool
        http_clients = self._rt._mcp_http_clients

        class _MergedCallTarget:
            async def call_tool(self, server, tool, arguments):
                # Try HTTP first (faster path), then stdio
                if server in http_clients:
                    return await http_clients[server].call_tool(tool, arguments)
                if pool is not None:
                    return await pool.call_tool(server, tool, arguments)
                raise RuntimeError(
                    f"MCP server {server!r} is not connected"
                )

        return _MergedCallTarget()

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
        if self._rt._mcp_pool is not None and self._rt._mcp_pool.started:
            for tdef in self._rt._mcp_pool.snapshot():
                from disco.tools.mcp.naming import split_qualified_name
                parts = split_qualified_name(tdef.name)
                if parts is None:
                    continue
                server, tool_name = parts
                mcp_entries.append({
                    "server": server,
                    "tool_name": tool_name,
                    "tool": tdef,
                })

        # HTTP tools
        for qname, tdef in self._rt._mcp_http_tools.items():
            from disco.tools.mcp.naming import split_qualified_name
            parts = split_qualified_name(qname)
            if parts is None:
                continue
            server, tool_name = parts
            mcp_entries.append({
                "server": server,
                "tool_name": tool_name,
                "tool": tdef,
            })

        if mcp_entries:
            self._rt._mcp_retrieval_searches, self._rt._mcp_retrieval_extractions = \
                build_retrieval_providers(
                    mcp_entries,
                    call_fn=self._mcp_call_target.call_tool,
                )
            _LOG.info(
                "MCP retrieval: built %d search + %d extraction provider(s)",
                len(self._rt._mcp_retrieval_searches),
                len(self._rt._mcp_retrieval_extractions),
            )
            # Drop any cached Build cap-handlers so the next build composes over
            # the freshly-built MCP providers (RP-05b §3).
            self._rt._cap_handlers = None

    async def _close_mcp_pool(self) -> None:
        if self._rt._mcp_pool is not None:
            await self._rt._mcp_pool.aclose()
            self._rt._mcp_pool = None
        for client in list(self._rt._mcp_http_clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._rt._mcp_http_clients.clear()
        self._rt._mcp_http_tools.clear()
        self._rt._mcp_retrieval_searches.clear()
        self._rt._mcp_retrieval_extractions.clear()
        self._rt._cap_handlers = None

    def mcp_approval_state(self) -> dict[str, dict]:
        """Return the pending approval state for WS frame dispatch (D3).

        Each key is a server name; value has 'old_hash' and 'new_hash'.
        """
        return dict(self._rt._mcp_approval_pending)

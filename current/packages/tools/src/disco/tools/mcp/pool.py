"""McpPool — RP-05 rung A (the live pool of MCP connections).

Lifecycle:
- start(): connect + initialize() for every enabled server. One task = one client
  lifecycle; AsyncExitStack per connection for cleanup.
- snapshot(): returns the frozen list of ToolDef objects for ONE conversation.
  list_changed notifications do NOT mutate the live list.
- aclose(): drain AsyncExitStack; kill stdio subprocesses.

Raises ApprovalRequired on start if any enabled server has no fresh approval.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp.types import Tool as MCPTool
from pydantic import BaseModel

from ..anatomy import ToolDef
from ..behavior import OPAQUE_MCP_BEHAVIOR
from .approval import (
    ApprovalRequired,
    ConfigApprovalRequired,
    compute_config_hash,
    compute_description_hash,
)
from .config import McpServerConfig, McpSettings
from .naming import qualified_name
from .stdio import McpStdioClient

_LOG = logging.getLogger(__name__)

_UNTRUSTED_DESC_WRAPPER = (
    '<untrusted_tool_description name="{name}">{desc}</untrusted_tool_description>'
)


def _leaf_exception_type(exc: BaseException) -> str:
    """Return a useful leaf exception type name, unwrapping ExceptionGroups.

    Mirrors the intent of `agent_server.mcp_transport._diagnostic_exception_type`
    (the streamable_http diagnostic's exception_type), kept as a small local
    copy rather than an import: `tools` sits below `agent_server` in the
    package layering and must not import upward.
    """
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _leaf_exception_type(exc.exceptions[0])
    return type(exc).__name__


class McpPool:
    """A managed pool of MCP server connections.

    Built once at agent-server start from the persisted config. The pool's
    snapshot() is called per conversation to freeze the tool set.
    """

    def __init__(
        self,
        settings: McpSettings,
        *,
        secrets: Any | None = None,  # SecretsStore
        approvals: dict[str, str] | None = None,  # server -> stored description_hash
        config_approvals: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._approvals = dict(approvals or {})
        self._config_approvals = dict(config_approvals or {})
        self._exit_stack = AsyncExitStack()
        self._clients: dict[str, McpStdioClient] = {}
        self._tools: dict[str, ToolDef] = {}  # qualified_name -> ToolDef
        self._server_status: dict[str, str] = {}  # server_name -> status
        # server_name -> {code, exception_type, attempts} — same shape as the
        # streamable_http transport's diagnostic (mcp_transport.py). Only
        # populated for servers whose status ends up "error": the previous
        # behavior surfaced no code/message anywhere but the container log.
        self._server_diagnostics: dict[str, dict[str, object]] = {}
        self._started = False
        # D3: per-server approval-pending info for WS frame dispatch
        self._approval_pending: dict[str, dict[str, str]] = {}

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Connect + initialize every enabled server. Populates _tools.

        Servers whose description_hash mismatches the stored approval are
        marked status="approval_required" and do NOT have their tools registered.
        On init timeout or failure, the server is marked status="error" — the
        rest of the pool runs MCP-less for that server.

        Returns a list of ApprovalRequired instances for any refused servers.
        """
        if self._started:
            return

        _LOG.info("McpPool.start: %d server(s) configured", len(self._settings.servers))

        for name, srv in self._settings.servers.items():
            if not srv.enabled:
                _LOG.debug("McpPool: server %r is disabled — skipping", name)
                self._server_status[name] = "disabled"
                continue

            # Only stdio transport in rung A; streamable_http arrives in rung B.
            if srv.transport == "streamable_http":
                _LOG.info("McpPool: server %r is streamable_http — deferred to rung B", name)
                self._server_status[name] = "error"
                continue

            try:
                current_config_hash = compute_config_hash(srv)
                stored_config_hash = self._config_approvals.get(name)
                if stored_config_hash != current_config_hash:
                    raise ConfigApprovalRequired(
                        name, stored_config_hash or "", current_config_hash
                    )
                await self._connect_stdio(name, srv)
            except ConfigApprovalRequired as exc:
                self._server_status[name] = "approval_required"
                self._approval_pending[name] = {
                    "kind": "config",
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                continue
            except ApprovalRequired as exc:
                # D1: per-server refusal — mark the server, don't propagate.
                # Other servers still start, and their tools are available.
                _LOG.warning(
                    "McpPool: server %r refused — description_hash changed "
                    "(%s → %s) — re-approval required",
                    name,
                    exc.old_hash[:12],
                    exc.new_hash[:12],
                )
                self._server_status[name] = "approval_required"
                self._approval_pending[name] = {
                    "kind": "tools",
                    "old_hash": exc.old_hash,
                    "new_hash": exc.new_hash,
                }
                # P1: teardown unapproved server — kill subprocess, remove from pool.
                # The client is still registered in _clients (connected before the
                # hash check in _connect_stdio). Disconnect it so no live subprocess
                # with resolved secrets survives for an unapproved server.
                client = self._clients.pop(name, None)
                if client is not None:
                    import contextlib

                    with contextlib.suppress(Exception):
                        await client.close()
                continue
            except Exception as exc:
                _LOG.warning("McpPool: server %r failed to start: %s", name, exc)
                self._server_status[name] = "error"
                self._server_diagnostics[name] = {
                    "code": "mcp_stdio_connect_failed",
                    "attempts": 1,
                    "exception_type": _leaf_exception_type(exc),
                }
                continue

            self._server_status[name] = "connected"

        self._started = True
        _LOG.info(
            "McpPool.start: done — %d tool(s) across %d server(s)",
            len(self._tools),
            sum(1 for s in self._server_status.values() if s == "connected"),
        )

    async def _connect_stdio(self, name: str, srv: McpServerConfig) -> None:
        """Connect one stdio server, build ToolDefs, verify approval hash."""
        resolved_env = _resolve_stdio_env(srv.env, self._secrets)
        client = McpStdioClient(**_stdio_launch_kwargs(srv, resolved_env))

        await self._exit_stack.enter_async_context(_ClientContext(name, client, self))

        await client.connect()
        self._clients[name] = client

        # List tools and build ToolDefs
        raw_tools: list[MCPTool] = await client.list_tools()

        _check_tool_approval(name, raw_tools, self._approvals)

        _register_stdio_tools(name, srv, raw_tools, self._tools)

    def snapshot(self) -> list[ToolDef]:
        """The frozen list of ToolDefs for one conversation.

        Returns a COPY — callers cannot mutate the pool's live state.
        """
        return list(self._tools.values())

    def server_status(self) -> dict[str, str]:
        """Per-server status: connected | disconnected | error | disabled | approval_required."""
        return dict(self._server_status)

    def server_diagnostics(self) -> dict[str, dict[str, object]]:
        """Per-server {code, attempts, exception_type} for servers in "error"
        status — the stdio counterpart of the streamable_http transport's
        diagnostic (mcp_transport.McpHttpConnector.status)."""
        return {name: dict(diag) for name, diag in self._server_diagnostics.items()}

    def approval_pending(self) -> dict[str, dict[str, str]]:
        """Return {server: {old_hash, new_hash}} for servers that need re-approval."""
        return dict(self._approval_pending)

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on a specific MCP server. Raises RuntimeError if the
        server is not connected."""
        client = self._clients.get(server)
        if client is None:
            raise RuntimeError(
                f"MCP server {server!r} is not connected; "
                f"status={self._server_status.get(server, 'unknown')}"
            )
        return await client.call_tool(tool, arguments)

    async def aclose(self) -> None:
        """Drain all connections, kill subprocesses."""
        _LOG.info("McpPool.aclose: draining %d client(s)", len(self._clients))
        self._clients.clear()
        self._tools.clear()
        await self._exit_stack.aclose()
        self._started = False


class _ClientContext:
    """Context manager that registers/exits a client in the pool's exit stack."""

    def __init__(self, name: str, client: McpStdioClient, pool: McpPool) -> None:
        self.name = name
        self.client = client
        self.pool = pool

    async def __aenter__(self) -> McpStdioClient:
        return self.client

    async def __aexit__(self, *args: Any) -> None:
        await self.client.close()
        self.pool._clients.pop(self.name, None)
        self.pool._server_status[self.name] = "disconnected"


def _resolve_stdio_env(env: dict[str, str] | None, secrets: Any | None) -> dict[str, str]:
    """Resolve a server's SecretRefs via the SecretsStore into a plain env dict."""
    resolved_env: dict[str, str] = {}
    if env and secrets:
        for key, ref in env.items():
            val = secrets.get(ref)
            if val is not None:
                resolved_env[key] = val
    return resolved_env


def _stdio_launch_kwargs(srv: McpServerConfig, resolved_env: dict[str, str]) -> dict[str, Any]:
    """Build the McpStdioClient constructor kwargs for one stdio server."""
    return {
        "command": list(srv.command or []),
        "args": list(srv.args or []),
        "env": resolved_env or None,
    }


def _check_tool_approval(name: str, raw_tools: list[MCPTool], approvals: dict[str, str]) -> str:
    """Compute the description hash for a server's tools and verify it against the
    stored approval. Returns the new hash; raises ApprovalRequired on mismatch."""
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
    return new_hash


def _register_stdio_tools(
    name: str,
    srv: McpServerConfig,
    raw_tools: list[MCPTool],
    tools: dict[str, ToolDef],
) -> None:
    """Register ToolDefs for one stdio server's tools into `tools` (in place).

    Only tools in allowed_tools (if set) are registered; disallowed and
    already-registered (duplicate) tools are skipped.
    """
    allowed = set(srv.allowed_tools) if srv.allowed_tools is not None else None

    for tool in raw_tools:
        if allowed is not None and tool.name not in allowed:
            _LOG.debug("McpPool: tool %r not in allowlist for server %r", tool.name, name)
            continue

        qname = qualified_name(name, tool.name)
        if qname in tools:
            _LOG.warning("McpPool: tool %r already registered — skipping", qname)
            continue

        # Wrap the description in the untrusted-text fence so the LLM
        # knows it arrived over MCP (not a hardcoded tool description).
        fenced_desc = _UNTRUSTED_DESC_WRAPPER.format(
            name=name, desc=tool.description or "(no description)"
        )

        tools[qname] = ToolDef(
            name=qname,
            description=fenced_desc,
            args_model=_schema_to_args_model(tool),
            needs=frozenset(),
            base_risk=srv.risk_tier,  # REQUIRED — not inferred
            runs_in="in_process",  # stdio subprocess executes on the host
            read_only=False,  # MCP tools are not assumed read-only
            uses_capabilities=frozenset(),
            behavior=OPAQUE_MCP_BEHAVIOR,
        )


def _schema_to_args_model(tool: MCPTool) -> type:
    """Create a Pydantic BaseModel from a tool's inputSchema.

    Uses pydantic.create_model to dynamically build the args model so it
    validates ToolCall.arguments before execution — same contract as every
    built-in tool (executor.py validates against args_model).
    """
    from pydantic import BaseModel, create_model

    schema = tool.inputSchema or {}
    if isinstance(schema, dict) and "properties" in schema:
        # Build field definitions from JSON schema properties
        fields: dict[str, Any] = {}
        for prop_name, prop_schema in schema.get("properties", {}).items():
            prop_type = _json_type_to_python(prop_schema)
            default = ... if prop_name in schema.get("required", []) else None
            fields[prop_name] = (prop_type, default)

        if not fields:
            # No parameters tool — still need a model
            return _EmptyArgs
        return create_model(
            f"_{tool.name}_args",
            __base__=BaseModel,
            **fields,
        )

    # No inputSchema, or non-standard shape — fall back to empty args
    return _EmptyArgs


class _EmptyArgs(BaseModel):
    """A no-arguments model for tools that take no parameters."""

    model_config = {"frozen": True, "extra": "ignore"}


def _json_type_to_python(prop: dict) -> type:
    """Map a JSON Schema property to a Python type hint."""
    typ = prop.get("type", "string")
    if isinstance(typ, list):
        # "type": ["string", "null"] — use the non-null type
        non_null = [t for t in typ if t != "null"]
        typ = non_null[0] if non_null else "string"

    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    # For Any-typed fields, use Any
    from typing import Any

    py_type = mapping.get(typ, Any)
    return py_type

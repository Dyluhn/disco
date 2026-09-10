"""MCP server status routes (RP-05 rung B): per-server health for the UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from disco.core.llm.config import RouterConfig
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.mcp import McpServerConfig
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ..runtime import ConversationRuntime


def _server_config(name: str, raw: object) -> tuple[McpServerConfig | None, dict[str, Any] | None]:
    """Normalize persisted MCP input without reflecting secret-bearing values.

    ConfigStore normally returns typed models, while migration fixtures and old
    settings files may still contain dictionaries. Status reporting is an
    observability boundary: one malformed entry must be visible, but must not
    crash or echo its command, headers, environment, or validation input.
    """

    if isinstance(raw, McpServerConfig) and raw.name == name:
        return raw, None
    payload = raw.model_dump() if isinstance(raw, McpServerConfig) else raw
    if not isinstance(payload, Mapping):
        return None, {
            "code": "invalid_mcp_server_config",
            "fields": ["server"],
        }
    try:
        # The mapping key is authoritative. A stale/malicious embedded name must
        # never make status for one server impersonate another server.
        return McpServerConfig.model_validate({**payload, "name": name}), None
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or "server"
                for error in exc.errors(include_url=False, include_context=False)
            }
        )
        return None, {
            "code": "invalid_mcp_server_config",
            "fields": fields,
        }


def _invalid_server_entry(name: str, diagnostic: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": False,
        "status": "error",
        "diagnostic": diagnostic,
        "approval_required": False,
    }


def _sanitize_diagnostic(diagnostic: object) -> dict[str, Any]:
    """Keep only the safe {code, attempts, exception_type} keys — never a raw
    exception message, command, or secret-bearing value."""

    if not isinstance(diagnostic, Mapping):
        return {}
    return {
        key: value
        for key, value in diagnostic.items()
        if key in {"code", "attempts", "exception_type"} and isinstance(value, (str, int))
    }


def _http_runtime_state(runtime: ConversationRuntime, name: str) -> dict[str, Any]:
    """Return only the manager's already-sanitized status projection."""

    raw = runtime.mcp._http_status.get(name, {})
    if not isinstance(raw, Mapping):
        return {}
    state: dict[str, Any] = {}
    status = raw.get("status")
    if isinstance(status, str):
        state["status"] = status
    diagnostic = _sanitize_diagnostic(raw.get("diagnostic"))
    if diagnostic:
        state["diagnostic"] = diagnostic
    return state


def _server_status_entry(
    runtime: ConversationRuntime,
    name: str,
    srv: Any,
    *,
    config_enabled: bool,
    pool_status: str,
    approval_pending: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The status projection for one configured, parseable MCP server.

    Extracted from ``list_mcp_servers`` so that endpoint stays inside the
    callable-complexity budget; the branching lives here where it belongs.
    """
    live = bool(config_enabled and srv.enabled)
    status = pool_status if live else "disabled"

    # HTTP servers override status from our own connector tracking. Stdio
    # servers keep the pool's status but may still carry a diagnostic (e.g. a
    # bad command) — same treatment, different source.
    live_state: dict[str, Any] = {}
    if live and srv.transport == "streamable_http":
        live_state = _http_runtime_state(runtime, name)
        status = (
            "approval_required"
            if name in approval_pending
            else str(live_state.get("status") or "disconnected")
        )
    elif live:
        live_state = _stdio_runtime_state(runtime, name)

    entry: dict[str, Any] = {
        "name": name,
        "transport": srv.transport,
        "enabled": srv.enabled,
        "status": status,
        "approval_required": name in approval_pending,
    }
    if name in approval_pending:
        entry["description_hash"] = approval_pending[name].get("new_hash", "")
        entry["old_description_hash"] = approval_pending[name].get("old_hash", "")
    if "diagnostic" in live_state:
        entry["diagnostic"] = live_state["diagnostic"]
    return entry


def _stdio_runtime_state(runtime: ConversationRuntime, name: str) -> dict[str, Any]:
    """The stdio counterpart of `_http_runtime_state`: a failed stdio server's
    {code, attempts, exception_type} from the pool, same sanitized shape.

    Before this, a stdio connect failure (e.g. a bad/misspelled command)
    surfaced only `{"status": "error"}` here — the real cause was visible
    nowhere but the container's stdout.
    """

    pool = runtime.mcp._pool
    get_diagnostics = getattr(pool, "server_diagnostics", None)
    if pool is None or get_diagnostics is None:
        return {}
    diagnostic = _sanitize_diagnostic(get_diagnostics().get(name))
    return {"diagnostic": diagnostic} if diagnostic else {}


def _resolve_live_status(
    runtime: ConversationRuntime,
    name: str,
    cfg: RouterConfig,
    srv: McpServerConfig,
) -> tuple[str, dict[str, Any]]:
    """Compute the single-server status + any live transport tracking state.

    Mirrors the per-server resolution `get_mcp_server_status` needs: disabled
    servers report "disabled"; enabled streamable_http servers defer to the
    manager's own sanitized tracking; other enabled (stdio) servers defer to
    the pool for status AND, on failure, for the same diagnostic shape.
    """

    status = "disabled" if not cfg.mcp.enabled or not srv.enabled else "disconnected"
    live_state: dict[str, Any] = {}
    if cfg.mcp.enabled and srv.enabled and srv.transport == "streamable_http":
        live_state = _http_runtime_state(runtime, name)
        status = str(live_state.get("status") or "disconnected")
    elif cfg.mcp.enabled and srv.enabled:
        if runtime.mcp._pool is not None:
            status = runtime.mcp._pool.server_status().get(name, "disconnected")
        live_state = _stdio_runtime_state(runtime, name)
    return status, live_state


def _apply_approval_projection(
    result: dict[str, Any],
    name: str,
    approval_pending: dict[str, dict],
    mcp_enabled: bool,
    srv_enabled: bool,
    live_state: dict[str, Any],
) -> None:
    """Layer the approval-pending projection onto a status `result`, in place."""

    if name in approval_pending:
        result["approval_required"] = True
        result["description_hash"] = approval_pending[name].get("new_hash", "")
        result["old_description_hash"] = approval_pending[name].get("old_hash", "")
        if mcp_enabled and srv_enabled:
            result["status"] = "approval_required"
    else:
        result["approval_required"] = False
    if "diagnostic" in live_state:
        result["diagnostic"] = live_state["diagnostic"]


def make_mcp_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/mcp/servers")
    async def list_mcp_servers() -> dict:
        """Per-server status projection for the UI (rung B).

        Returns the live status of each configured MCP server:
        connected, disconnected, error, disabled, or approval_required.
        HTTP servers reveal their resolved transport info.
        """
        if runtime is None:
            return {"servers": {}}
        cfg = runtime._config_store.load()
        mcp_cfg = cfg.mcp

        servers: dict[str, dict] = {}
        srv_status = runtime.mcp._pool.server_status() if runtime.mcp._pool else {}
        approval_pending = runtime.mcp.mcp_approval_state()

        for name, srv_raw in mcp_cfg.servers.items():
            srv, diagnostic = _server_config(name, srv_raw)
            if srv is None:
                servers[name] = _invalid_server_entry(name, diagnostic or {})
                continue
            servers[name] = _server_status_entry(
                runtime,
                name,
                srv,
                config_enabled=mcp_cfg.enabled,
                pool_status=srv_status.get(name, "disconnected"),
                approval_pending=approval_pending,
            )

        return {"enabled": mcp_cfg.enabled, "servers": servers}

    @router.post("/api/mcp/reload")
    async def reload_mcp_servers() -> dict:
        """Apply the latest persisted MCP config/approvals to the live runtime."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return await runtime.mcp.reload()

    @router.get("/api/mcp/servers/{name}/status")
    async def get_mcp_server_status(name: str) -> dict:
        """Per-server health/status endpoint for the UI status projection."""
        if runtime is None:
            return {"name": name, "status": "disconnected", "reason": "no runtime"}
        cfg = runtime._config_store.load()
        srv_raw = cfg.mcp.servers.get(name)
        if srv_raw is None:
            raise HTTPException(status_code=404, detail={"reason": "server_not_found"})
        srv, diagnostic = _server_config(name, srv_raw)
        if srv is None:
            return _invalid_server_entry(name, diagnostic or {})

        status, live_state = _resolve_live_status(runtime, name, cfg, srv)

        approval_pending = runtime.mcp.mcp_approval_state()
        result: dict = {
            "name": name,
            "transport": srv.transport,
            "enabled": srv.enabled,
            "status": status,
        }
        _apply_approval_projection(
            result, name, approval_pending, cfg.mcp.enabled, srv.enabled, live_state
        )

        return result

    return router

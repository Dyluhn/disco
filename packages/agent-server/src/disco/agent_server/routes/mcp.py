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


def _http_runtime_state(runtime: ConversationRuntime, name: str) -> dict[str, Any]:
    """Return only the manager's already-sanitized status projection."""

    raw = runtime.mcp._http_status.get(name, {})
    if not isinstance(raw, Mapping):
        return {}
    state: dict[str, Any] = {}
    status = raw.get("status")
    if isinstance(status, str):
        state["status"] = status
    diagnostic = raw.get("diagnostic")
    if isinstance(diagnostic, Mapping):
        state["diagnostic"] = {
            key: value
            for key, value in diagnostic.items()
            if key in {"code", "attempts", "exception_type"} and isinstance(value, (str, int))
        }
    return state


def _resolve_live_status(
    runtime: ConversationRuntime,
    name: str,
    cfg: RouterConfig,
    srv: McpServerConfig,
) -> tuple[str, dict[str, Any]]:
    """Compute the single-server status + any live HTTP tracking state.

    Mirrors the per-server resolution `get_mcp_server_status` needs: disabled
    servers report "disabled"; enabled streamable_http servers defer to the
    manager's own sanitized tracking; other enabled servers defer to the pool.
    """

    status = "disabled" if not cfg.mcp.enabled or not srv.enabled else "disconnected"
    live_http: dict[str, Any] = {}
    if cfg.mcp.enabled and srv.enabled and srv.transport == "streamable_http":
        live_http = _http_runtime_state(runtime, name)
        status = str(live_http.get("status") or "disconnected")
    elif cfg.mcp.enabled and srv.enabled:
        if runtime.mcp._pool is not None:
            status = runtime.mcp._pool.server_status().get(name, "disconnected")
    return status, live_http


def _apply_approval_projection(
    result: dict[str, Any],
    name: str,
    approval_pending: dict[str, dict],
    mcp_enabled: bool,
    srv_enabled: bool,
    live_http: dict[str, Any],
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
    if "diagnostic" in live_http:
        result["diagnostic"] = live_http["diagnostic"]


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
            status = (
                "disabled"
                if not mcp_cfg.enabled or not srv.enabled
                else srv_status.get(name, "disconnected")
            )
            # HTTP servers: override status from our own tracking
            live_http: dict[str, Any] = {}
            if mcp_cfg.enabled and srv.enabled and srv.transport == "streamable_http":
                live_http = _http_runtime_state(runtime, name)
                if name in approval_pending:
                    status = "approval_required"
                else:
                    status = str(live_http.get("status") or "disconnected")

            entry: dict = {
                "name": name,
                "transport": srv.transport,
                "enabled": srv.enabled,
                "status": status,
            }
            if name in approval_pending:
                entry["approval_required"] = True
                entry["description_hash"] = approval_pending[name].get("new_hash", "")
                entry["old_description_hash"] = approval_pending[name].get("old_hash", "")
            else:
                entry["approval_required"] = False
            if "diagnostic" in live_http:
                entry["diagnostic"] = live_http["diagnostic"]

            servers[name] = entry

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

        status, live_http = _resolve_live_status(runtime, name, cfg, srv)

        approval_pending = runtime.mcp.mcp_approval_state()
        result: dict = {
            "name": name,
            "transport": srv.transport,
            "enabled": srv.enabled,
            "status": status,
        }
        _apply_approval_projection(
            result, name, approval_pending, cfg.mcp.enabled, srv.enabled, live_http
        )

        return result

    return router

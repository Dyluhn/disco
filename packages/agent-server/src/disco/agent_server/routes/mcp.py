"""MCP server status routes (RP-05 rung B): per-server health for the UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
        if not mcp_cfg.enabled:
            return {"enabled": False, "servers": {}}

        servers: dict[str, dict] = {}
        srv_status = runtime._mcp_pool.server_status() if runtime._mcp_pool else {}
        approval_pending = runtime.mcp_approval_state()

        for name, srv_raw in mcp_cfg.servers.items():
            srv, diagnostic = _server_config(name, srv_raw)
            if srv is None:
                servers[name] = _invalid_server_entry(name, diagnostic or {})
                continue
            status = "disabled" if not srv.enabled else srv_status.get(name, "disconnected")
            # HTTP servers: override status from our own tracking
            if srv.enabled and srv.transport == "streamable_http":
                if name in runtime._mcp_http_clients:
                    status = "connected"
                elif name in approval_pending:
                    status = "approval_required"
                else:
                    # Not yet started or failed
                    status = status if status != "disconnected" else "disconnected"

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

            servers[name] = entry

        return {"enabled": True, "servers": servers}

    @router.post("/api/mcp/reload")
    async def reload_mcp_servers() -> dict:
        """Apply the latest persisted MCP config/approvals to the live runtime."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return await runtime.reload_mcp_pool()

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

        status = "disabled" if not srv.enabled else "disconnected"
        if srv.enabled and srv.transport == "streamable_http":
            if name in runtime._mcp_http_clients:
                status = "connected"
            else:
                status = "disconnected"
        elif srv.enabled:
            if runtime._mcp_pool is not None:
                status = runtime._mcp_pool.server_status().get(name, "disconnected")

        approval_pending = runtime.mcp_approval_state()
        result: dict = {
            "name": name,
            "transport": srv.transport,
            "enabled": srv.enabled,
            "status": status,
        }
        if name in approval_pending:
            result["approval_required"] = True
            result["description_hash"] = approval_pending[name].get("new_hash", "")
            result["old_description_hash"] = approval_pending[name].get("old_hash", "")
        else:
            result["approval_required"] = False

        return result

    return router

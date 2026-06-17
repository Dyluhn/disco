"""MCP server status routes (RP-05 rung B): per-server health for the UI."""

from __future__ import annotations

from typing import cast

from disco.core.store.sqlite import SqliteEventStore
from disco.tools.mcp import McpServerConfig
from fastapi import APIRouter, HTTPException

from ..runtime import ConversationRuntime


def make_mcp_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
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
            # RouterConfig.mcp.servers is typed `dict[str, dict]` (loose
            # settings storage) but at runtime each entry is an McpServerConfig
            # — cast to that so attribute access type-checks.
            srv = cast(McpServerConfig, srv_raw)
            status = srv_status.get(name, "disconnected")
            # HTTP servers: override status from our own tracking
            if srv.transport == "streamable_http":
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

    @router.get("/api/mcp/servers/{name}/status")
    async def get_mcp_server_status(name: str) -> dict:
        """Per-server health/status endpoint for the UI status projection."""
        if runtime is None:
            return {"name": name, "status": "disconnected", "reason": "no runtime"}
        cfg = runtime._config_store.load()
        srv_raw = cfg.mcp.servers.get(name)
        if srv_raw is None:
            raise HTTPException(status_code=404, detail={"reason": "server_not_found"})
        # RouterConfig.mcp.servers is typed `dict[str, dict]` (loose
        # settings storage) but at runtime each entry is an McpServerConfig
        # — cast to that so attribute access type-checks.
        srv = cast(McpServerConfig, srv_raw)

        status = "disconnected"
        if srv.transport == "streamable_http":
            if name in runtime._mcp_http_clients:
                status = "connected"
            else:
                status = "disconnected"
        else:
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

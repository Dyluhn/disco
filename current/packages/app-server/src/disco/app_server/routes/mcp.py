"""MCP connection routes — live, persistent CRUD + approval (rung B)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config.dtos import (
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    McpServerPatchDTO,
)
from ..config_state import ConfigState


class _McpImportBody(BaseModel):
    """Paste-a-config import request: the raw pasted text, verbatim.

    `dry_run=True` (the default) parses + validates only — the preview the
    Settings paste box renders. `dry_run=False` stores pasted secret values in
    the SecretStore (as refs) and creates the servers through the same service
    path as the manual form.
    """

    text: str = Field(max_length=64 * 1024)
    dry_run: bool = True


def make_mcp_router(state: ConfigState) -> APIRouter:
    router = APIRouter()

    @router.get("/api/mcp")
    async def get_mcp() -> list[McpConnectionDTO]:
        return state.mcp_connections()

    @router.post("/api/mcp/servers", status_code=201)
    async def create_mcp_server(body: McpServerConfigDTO) -> McpConnectionDTO:
        try:
            return state.create_mcp_server(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/mcp/servers/import")
    async def import_mcp_servers(body: _McpImportBody) -> dict:
        """Parse a pasted `mcpServers` JSON blob; preview or create servers.

        Registered BEFORE the /{name} routes so "import" is never captured as
        a server name. A completely uninterpretable paste is a 400 whose
        detail names what is missing; per-server problems come back as
        `servers[i].error` at 200 so the preview can render partial success.
        """
        try:
            return state._mcp_service.import_mcp_config(body.text, dry_run=body.dry_run)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/mcp/servers/{name}")
    async def update_mcp_server(name: str, body: McpServerPatchDTO) -> McpConnectionDTO:
        try:
            result = state.update_mcp_server(name, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}")
        return result

    @router.delete("/api/mcp/servers/{name}", status_code=204)
    async def delete_mcp_server(name: str) -> None:
        if not state.delete_mcp_server(name):
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}")

    @router.post("/api/mcp/servers/{name}/approve")
    async def approve_mcp_server(name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        try:
            return state.approve_mcp_server(name, body)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/api/mcp/servers/{name}/revoke")
    async def revoke_mcp_server(name: str) -> McpConnectionDTO:
        result = state.revoke_mcp_server(name)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}")
        return result

    return router

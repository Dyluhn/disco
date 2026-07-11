"""MCP connection routes — live, persistent CRUD + approval (rung B)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config.dtos import (
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    McpServerPatchDTO,
)
from ..config_state import ConfigState


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

    @router.patch("/api/mcp/servers/{name}")
    async def update_mcp_server(name: str, body: McpServerPatchDTO) -> McpConnectionDTO:
        result = state.update_mcp_server(name, body)
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

    return router

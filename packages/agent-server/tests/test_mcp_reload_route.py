from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from disco.agent_server.routes.mcp import make_mcp_router
from fastapi import FastAPI


async def test_reload_route_applies_persisted_mcp_settings_to_runtime() -> None:
    runtime = MagicMock()
    runtime.mcp.reload = AsyncMock(
        return_value={
            "ok": True,
            "configured_servers": ["tools"],
            "connected_servers": ["tools"],
            "registered_tools": ["mcp__tools__lookup"],
            "approval_required": [],
        }
    )
    app = FastAPI()
    app.include_router(make_mcp_router(MagicMock(), runtime))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/mcp/reload")

    assert response.status_code == 200
    assert response.json()["registered_tools"] == ["mcp__tools__lookup"]
    runtime.mcp.reload.assert_awaited_once_with()


async def test_reload_route_fails_honestly_without_runtime() -> None:
    app = FastAPI()
    app.include_router(make_mcp_router(MagicMock(), None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/mcp/reload")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "no_runtime"

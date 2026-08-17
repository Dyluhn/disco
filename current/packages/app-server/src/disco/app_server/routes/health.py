"""Health/liveness route — the ops probe hits this."""

from __future__ import annotations

from fastapi import APIRouter


def make_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "service": "app-server"}

    return router

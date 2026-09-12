"""Health/liveness route — the ops probe hits this."""

from __future__ import annotations

from disco.core.build_info import build_info
from fastapi import APIRouter


def make_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    async def health() -> dict:
        info = build_info()
        return {
            "status": "ok",
            "service": "app-server",
            "version": info.label(),
            "build": info.as_dict(),
        }

    return router

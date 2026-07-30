"""Health/liveness route — the canary + ops probe hit this."""

from __future__ import annotations

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime


def make_health_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> JSONResponse:
        """Cheap readiness probe: store reachable + runtime/router wired. Does NOT
        call a model (that's the canary's job — `harness/canary.py` adds a real
        grounded research query on top). Returns 200 ok / 503 degraded so a
        systemd-timer or uptime check can alert on a dead dependency."""
        checks: dict[str, object] = {}
        ok = True
        try:
            await store.list_conversations(owner_id=DEFAULT_OWNER_ID, limit=1)
            checks["store"] = "ok"
        except Exception as e:  # noqa: BLE001 — any store failure is a health signal
            checks["store"] = f"error: {e}"
            ok = False
        if runtime is None:
            checks["runtime"] = "absent (wire-only mode)"
        else:
            try:
                models = runtime._drivers.catalog().get("models", [])
                checks["models"] = len(models) if isinstance(models, list) else 0
                checks["runtime"] = "ok"
            except Exception as e:  # noqa: BLE001
                checks["runtime"] = f"error: {e}"
                ok = False
        body = {"status": "ok" if ok else "degraded", "version": "0.1.0", "checks": checks}
        return JSONResponse(body, status_code=200 if ok else 503)

    return router

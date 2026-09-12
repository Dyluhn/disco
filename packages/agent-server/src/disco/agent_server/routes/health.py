"""Health/liveness route — the canary + ops probe hit this."""

from __future__ import annotations

from disco.core import DEFAULT_OWNER_ID
from disco.core.build_info import build_info
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime


async def health_snapshot(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> tuple[dict[str, object], bool]:
    """Store reachable + runtime/router wired, plus the build identity. Never calls a
    model (that's the canary's job). Returns (body, ok); shared by `/health` and
    `/api/diagnostics` so the two can never disagree."""
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
            models = runtime.drivers.catalog().get("models", [])
            checks["models"] = len(models) if isinstance(models, list) else 0
            checks["runtime"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["runtime"] = f"error: {e}"
            ok = False
    info = build_info()
    body: dict[str, object] = {
        "status": "ok" if ok else "degraded",
        "version": info.label(),
        "build": info.as_dict(),
        "checks": checks,
    }
    return body, ok


def make_health_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> JSONResponse:
        """Cheap readiness probe; 200 ok / 503 degraded so a systemd timer or uptime
        check can alert on a dead dependency."""
        body, ok = await health_snapshot(store, runtime)
        return JSONResponse(body, status_code=200 if ok else 503)

    return router

"""`GET /api/diagnostics` — what Settings → Diagnostics shows and copies.

The in-process half of the doctor: build identity, this server's health snapshot,
the active sandbox probe, the local checks (config, data disk, database, secret key)
and the DISCO_* configuration with secret values redacted. No model call, no
subprocess; a few hundred milliseconds. Owner-scoped like every /api route.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime

from disco.core.build_info import build_info
from disco.core.evidence.schema import redact
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter

from ..doctor import local_checks, redacted_env
from ..runtime import ConversationRuntime
from .health import health_snapshot


def make_diagnostics_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/diagnostics")
    async def diagnostics() -> dict:
        health, _ok = await health_snapshot(store, runtime)
        if runtime is None:
            sandbox = {"reachable": False, "backend": "unknown", "detail": "runtime unavailable"}
        else:
            reachable, backend, detail = await runtime.sandbox.probe_active_sandbox()
            sandbox = {"reachable": reachable, "backend": backend, "detail": detail}
        env = dict(os.environ)
        info = build_info()
        return redact(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "version": info.label(),
                "build": info.as_dict(),
                "agent_server": health,
                "sandbox": sandbox,
                "checks": [asdict(c) for c in local_checks(env)],
                "env": redacted_env(env),
            }
        )

    return router

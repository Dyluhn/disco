"""Activity dashboard route — running tasks + scheduled-run history."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Query, Request

from ..auth import current_owner_id
from ..runtime import ConversationRuntime


async def _recent_runs(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    owner_id: str,
    limit: int,
) -> list[dict]:
    rows = runtime.schedules.list_recent_schedule_runs(owner_id=owner_id, limit=limit)
    for run in runtime.schedules.list_workflow_schedule_runs(owner_id=owner_id, limit=limit):
        cid = run["run_cid"]
        if not await store.conversation_owned_by(cid, owner_id):
            continue
        rows.append(
            {
                "run_id": run["run_id"],
                "schedule_id": run["schedule_id"],
                "conversation_id": cid,
                "fired_at": run["fired_at"],
                "coalesced": int(run["coalesced"]),
                "description": "Scheduled workflow",
                "title": await store.get_title(cid),
            }
        )
    return sorted(rows, key=lambda row: row["fired_at"], reverse=True)[:limit]


def make_activity_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/activity")
    async def get_activity(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict:
        """The background-task dashboard feed for one owner:
        - `running`: conversations with a LIVE run task right now, enriched with
          title/status/surface (the runtime is ground truth; cached status can lag).
        - `recent_runs`: recent scheduled-run history (newest first).
        - `counts.running`: the global "N tasks running" indicator value.
        Empty/zeroed (never an error) when there's no runtime or nothing is running."""
        if runtime is None:
            return {"running": [], "recent_runs": [], "counts": {"running": 0}}

        owner_id = current_owner_id(request)
        live = set(runtime.run_registry.active_conversation_ids())
        running: list[dict] = []
        if live:
            summaries = await store.list_conversation_summaries(
                owner_id=owner_id, limit=500, cursor=None
            )
            by_id = {s.conversation_id: s for s in summaries}
            # owner-scoping IS the security boundary: only running cids that belong to
            # this owner (present in their summaries) are surfaced.
            for cid in live:
                s = by_id.get(cid)
                if s is None:
                    continue
                running.append(
                    {
                        "id": cid,
                        "title": s.title or "(untitled)",
                        "status": s.status,
                        "surface": s.surface,
                        "created_at": s.created_at,
                    }
                )

        recent_runs = await _recent_runs(store, runtime, owner_id, limit)
        return {
            "running": running,
            "recent_runs": recent_runs,
            "counts": {"running": len(running)},
        }

    return router

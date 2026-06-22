"""Scheduled-task routes (RP-08): cron-style recurring conversation runs."""

from __future__ import annotations

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException

from ..runtime import ConversationRuntime
from ..schedule_models import CreateScheduleBody, PreviewScheduleBody
from ._common import _reject_if_imported


def make_schedules_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/schedules")
    async def create_schedule(
        conversation_id: str,
        body: CreateScheduleBody,
    ) -> dict:
        """Create a cron-style recurring schedule for a conversation.

        The cron expression in `rrule` is validated by cronsim; an invalid
        expression returns 422 (never silent — the user must fix it)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        _reject_if_imported(store, conversation_id)  # a schedule would revive a read-only import
        try:
            # P3: seed from last-selected when the schedule has no explicit
            # model_override. An explicit schedule pick stays authoritative.
            schedule_model = body.model_override
            if not schedule_model:
                last = runtime.get_last_selected_model()
                if last:
                    cfg = runtime._config_store.load()
                    if last in cfg.models:
                        schedule_model = last
            result = runtime.create_schedule(
                conversation_id=conversation_id,
                owner_id=DEFAULT_OWNER_ID,
                rrule=body.rrule,
                description=body.description,
                depth=body.depth,
                model_override=schedule_model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
        return result

    @router.get("/api/conversations/{conversation_id}/schedules")
    async def list_schedules_for_conversation(conversation_id: str) -> dict:
        """List all schedules for a conversation."""
        if runtime is None:
            return {"schedules": []}
        return {
            "schedules": runtime.list_schedules(
                owner_id=DEFAULT_OWNER_ID, conversation_id=conversation_id
            )
        }

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict:
        """Delete a schedule by id. OWNER-SCOPED. Returns `{deleted: true/false}`."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        deleted = runtime.delete_schedule(schedule_id, owner_id=DEFAULT_OWNER_ID)
        return {"ok": deleted, "schedule_id": schedule_id, "deleted": deleted}

    @router.post("/api/conversations/{conversation_id}/schedules/{schedule_id}/fire-now")
    async def fire_schedule_now(conversation_id: str, schedule_id: str) -> dict:
        """Run a schedule IMMEDIATELY (gap #98 — the 'fire now' control + the verify
        fire-now seam). Reuses the periodic execute path: emits a ScheduleRunEvent,
        re-injects the original query, kicks the loop. 404 if no such schedule."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        fired = await runtime.fire_schedule_now(schedule_id, owner_id=DEFAULT_OWNER_ID)
        if not fired:
            raise HTTPException(status_code=404, detail={"reason": "schedule_not_found"})
        return {"ok": True, "schedule_id": schedule_id, "fired": True}

    @router.post("/api/schedules/preview")
    async def preview_schedule(body: PreviewScheduleBody) -> dict:
        """Preview the next N run times for a cron expression.  Use this before
        saving a schedule — the confirm card shows next-3-runs to the user."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        times = runtime.preview_schedule_runs(body.rrule, body.n)
        if not times:
            raise HTTPException(
                status_code=422,
                detail={"reason": f"Invalid or non-firing cron expression: {body.rrule!r}"},
            )
        return {"next_runs": times, "rrule": body.rrule}

    return router

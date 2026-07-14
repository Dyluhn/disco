"""Scheduled-task routes (RP-08): cron-style recurring conversation runs."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import ScheduleSpec
from fastapi import APIRouter, HTTPException, Request

from ..auth import current_owner_id, current_session
from ..runtime import ConversationRuntime
from ..schedule_models import CreateScheduleBody, PreviewScheduleBody
from ._common import _reject_if_imported, require_owned_conversation


def make_schedules_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/conversations/{conversation_id}/schedules")
    async def create_schedule(
        conversation_id: str,
        body: CreateScheduleBody,
        request: Request,
    ) -> dict:
        """Create a cron-style recurring schedule for a conversation.

        The cron expression in `rrule` is validated by cronsim; an invalid
        expression returns 422 (never silent — the user must fix it)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
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
                owner_id=current_owner_id(request),
                rrule=body.rrule,
                description=body.description,
                timezone=body.timezone,
                depth=body.depth,
                model_override=schedule_model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
        return result

    @router.get("/api/conversations/{conversation_id}/schedules")
    async def list_schedules_for_conversation(conversation_id: str, request: Request) -> dict:
        """List all schedules for a conversation."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"schedules": []}
        return {
            "schedules": runtime.list_schedules(
                owner_id=current_owner_id(request), conversation_id=conversation_id
            )
        }

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request) -> dict:
        """Delete a schedule by id. OWNER-SCOPED. Returns `{deleted: true/false}`."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        deleted = runtime.delete_schedule(schedule_id, owner_id=current_owner_id(request))
        return {"ok": deleted, "schedule_id": schedule_id, "deleted": deleted}

    @router.post("/api/conversations/{conversation_id}/schedules/{schedule_id}/fire-now")
    async def fire_schedule_now(
        conversation_id: str, schedule_id: str, request: Request
    ) -> dict:
        """Run a schedule IMMEDIATELY (gap #98 — the 'fire now' control + the verify
        fire-now seam). Reuses the periodic execute path: emits a ScheduleRunEvent,
        re-injects the original query, kicks the loop. 404 if no such schedule."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        await require_owned_conversation(request, store, conversation_id)
        fired = await runtime.fire_schedule_now(schedule_id, owner_id=current_owner_id(request))
        if not fired:
            raise HTTPException(status_code=404, detail={"reason": "schedule_not_found"})
        return {"ok": True, "schedule_id": schedule_id, "fired": True}

    @router.post("/api/schedules/preview")
    async def preview_schedule(body: PreviewScheduleBody) -> dict:
        """Preview the next N run times for a cron expression.  Use this before
        saving a schedule — the confirm card shows next-3-runs to the user."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        times = runtime.preview_schedule_runs(
            body.rrule,
            body.n,
            timezone=body.timezone,
        )
        if not times:
            raise HTTPException(
                status_code=422,
                detail={"reason": f"Invalid or non-firing cron expression: {body.rrule!r}"},
            )
        return {"next_runs": times, "rrule": body.rrule, "timezone": body.timezone}

    @router.post("/api/workflows/schedules")
    async def create_workflow_schedule(body: ScheduleSpec, request: Request) -> dict:
        """Create a sealed recurring workflow schedule.

        This is intentionally additive to the existing conversation schedules:
        workflow schedules fire fresh sealed agent conversations rather than
        appending to an existing conversation.
        """
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        try:
            return runtime.create_workflow_schedule(
                body,
                owner_id=current_owner_id(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc

    @router.get("/api/workflows/schedules")
    async def list_workflow_schedules(request: Request) -> dict:
        if runtime is None:
            return {"schedules": []}
        session = current_session(request)
        return {
            "schedules": runtime.list_workflow_schedules(
                owner_id=session.owner_id,
                include_unclaimed_legacy=session.is_admin,
            )
        }

    @router.get("/api/workflows/schedules/runs")
    async def list_workflow_schedule_runs(
        request: Request,
        schedule_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        if runtime is None:
            return {"runs": []}
        session = current_session(request)
        return {
            "runs": runtime.list_workflow_schedule_runs(
                schedule_id=schedule_id,
                owner_id=session.owner_id,
                include_unclaimed_legacy=session.is_admin,
                limit=limit,
            )
        }

    @router.post("/api/workflows/schedules/{schedule_id}/fire-now")
    async def fire_workflow_schedule_now(schedule_id: str, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        session = current_session(request)
        record = await runtime.fire_workflow_schedule_now(
            schedule_id,
            owner_id=session.owner_id,
            include_unclaimed_legacy=session.is_admin,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": "workflow_schedule_not_found"},
            )
        return {"ok": True, "schedule_id": schedule_id, "run": record}

    return router

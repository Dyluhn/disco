"""Scheduled-task routes (RP-08): cron-style recurring conversation runs."""

from __future__ import annotations

from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import ScheduleSpec
from fastapi import APIRouter, HTTPException, Request

from ..auth import current_owner_id, current_session
from ..runtime import ConversationRuntime
from ..schedule_models import CreateScheduleBody, PreviewScheduleBody
from ._common import _reject_if_imported, require_owned_conversation


async def _create_schedule_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    body: CreateScheduleBody,
    owner_id: str,
) -> dict:
    try:
        # P3: seed from last-selected when the schedule has no explicit
        # model_override. An explicit schedule pick stays authoritative.
        schedule_model = body.model_override
        if not schedule_model:
            last = runtime.settings.model_binding.get_last_selected_model()
            if last:
                cfg = runtime._config_store.load()
                if last in cfg.models:
                    schedule_model = last
        result = runtime.schedules.create_schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=body.rrule,
            description=body.description,
            timezone=body.timezone,
            depth=body.depth,
            model_override=schedule_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    return result


def _delete_schedule_response(
    runtime: ConversationRuntime, schedule_id: str, owner_id: str
) -> dict:
    deleted = runtime.schedules.delete_schedule(schedule_id, owner_id=owner_id)
    return {"ok": deleted, "schedule_id": schedule_id, "deleted": deleted}


async def _fire_schedule_now_response(
    runtime: ConversationRuntime, schedule_id: str, owner_id: str
) -> dict:
    fired = await runtime.schedules.fire_now(schedule_id, owner_id=owner_id)
    if not fired:
        raise HTTPException(status_code=404, detail={"reason": "schedule_not_found"})
    return {"ok": True, "schedule_id": schedule_id, "fired": True}


def _preview_schedule_response(runtime: ConversationRuntime, body: PreviewScheduleBody) -> dict:
    times = runtime.schedules.preview_schedule_runs(
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


def _create_workflow_schedule_response(
    runtime: ConversationRuntime, body: ScheduleSpec, owner_id: str
) -> dict:
    try:
        return runtime.schedules.create_workflow_schedule(body, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc


def _list_workflow_schedules_response(runtime: ConversationRuntime, session: AuthSession) -> dict:
    return {
        "schedules": runtime.schedules.list_workflow_schedules(
            owner_id=session.owner_id,
            include_unclaimed_legacy=session.is_admin,
        )
    }


def _list_workflow_schedule_runs_response(
    runtime: ConversationRuntime,
    session: AuthSession,
    schedule_id: str | None,
    limit: int,
) -> dict:
    return {
        "runs": runtime.schedules.list_workflow_schedule_runs(
            schedule_id=schedule_id,
            owner_id=session.owner_id,
            include_unclaimed_legacy=session.is_admin,
            limit=limit,
        )
    }


async def _fire_workflow_schedule_now_response(
    runtime: ConversationRuntime, schedule_id: str, session: AuthSession
) -> dict:
    record = await runtime.schedules.fire_workflow_schedule_now(
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
        """Create a recurring schedule, returning 422 for an invalid cron."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        _reject_if_imported(store, conversation_id)  # a schedule would revive a read-only import
        return await _create_schedule_response(
            runtime, conversation_id, body, current_owner_id(request)
        )

    @router.get("/api/conversations/{conversation_id}/schedules")
    async def list_schedules_for_conversation(conversation_id: str, request: Request) -> dict:
        """List all schedules for a conversation."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"schedules": []}
        return {
            "schedules": runtime.schedules.list_schedules(
                owner_id=current_owner_id(request), conversation_id=conversation_id
            )
        }

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, request: Request) -> dict:
        """Delete a schedule by id. OWNER-SCOPED. Returns `{deleted: true/false}`."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return _delete_schedule_response(runtime, schedule_id, current_owner_id(request))

    @router.post("/api/conversations/{conversation_id}/schedules/{schedule_id}/fire-now")
    async def fire_schedule_now(conversation_id: str, schedule_id: str, request: Request) -> dict:
        """Fire a schedule through its periodic execution path, or return 404."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        # Firing wakes a sandbox and kicks the loop, exactly like creating one.
        _reject_if_imported(store, conversation_id)
        return await _fire_schedule_now_response(runtime, schedule_id, current_owner_id(request))

    @router.post("/api/schedules/preview")
    async def preview_schedule(body: PreviewScheduleBody) -> dict:
        """Preview the next N run times for a cron expression."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return _preview_schedule_response(runtime, body)

    @router.post("/api/workflows/schedules")
    async def create_workflow_schedule(body: ScheduleSpec, request: Request) -> dict:
        """Create a sealed schedule that fires fresh workflow conversations."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return _create_workflow_schedule_response(runtime, body, current_owner_id(request))

    @router.get("/api/workflows/schedules")
    async def list_workflow_schedules(request: Request) -> dict:
        if runtime is None:
            return {"schedules": []}
        return _list_workflow_schedules_response(runtime, current_session(request))

    @router.get("/api/workflows/schedules/runs")
    async def list_workflow_schedule_runs(
        request: Request,
        schedule_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        if runtime is None:
            return {"runs": []}
        return _list_workflow_schedule_runs_response(
            runtime, current_session(request), schedule_id, limit
        )

    @router.post("/api/workflows/schedules/{schedule_id}/fire-now")
    async def fire_workflow_schedule_now(schedule_id: str, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        return await _fire_workflow_schedule_now_response(
            runtime, schedule_id, current_session(request)
        )

    return router

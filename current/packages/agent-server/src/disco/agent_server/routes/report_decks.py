"""Direct ReportEvent → structured authored-deck handoff route.

The route owns only the source/authority boundary. Execution is supplied by the
existing run/tool supervisor through a narrow port so this module cannot create
a second agent runner or accept client-authored report content. App registration
and the port implementation remain parent-owned fan-in work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from uuid import uuid4

from disco.core import (
    ConversationState,
    ConversationStatus,
    ReportDeckInvocationEvent,
    ReportEvent,
    ToolCall,
)
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..report_deck_handoff import ReportDeckJob, build_report_deck_job
from ..runtime import ConversationRuntime
from ._common import require_owned_conversation


class ReportDeckStartPort(Protocol):
    """Existing-supervision adapter used by the route's parent fan-in."""

    async def start_report_deck(
        self,
        source_conversation_id: str,
        job: ReportDeckJob,
    ) -> str: ...


PersistReportDeckSource = Callable[[str, str, ReportDeckJob], Awaitable[None]]
ScheduleReportDeckTool = Callable[[str, ReportDeckJob, ToolCall], Awaitable[None]]


class DirectReportDeckStartPort:
    """Create and admit one owner-scoped deck operation.

    The two callbacks are the existing host seams: the first persists/copies the
    bounded authoritative source, and the second registers the operation with the
    normal run supervisor/event executor. This class deliberately has no model
    planner or generic tool-selection step; its one callable is ``slides_generate``.
    """

    def __init__(
        self,
        store: SqliteEventStore,
        runtime: ConversationRuntime,
        *,
        persist_source: PersistReportDeckSource,
        schedule_tool: ScheduleReportDeckTool,
        target_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._persist_source = persist_source
        self._schedule_tool = schedule_tool
        self._target_id_factory = target_id_factory or (lambda: f"deck_{uuid4().hex}")

    async def start_report_deck(self, source_conversation_id: str, job: ReportDeckJob) -> str:
        owner_id = await self._store.conversation_owner_id(source_conversation_id)
        if owner_id is None:
            raise ValueError("source conversation has no owner")

        target_id = self._target_id_factory()
        self._store.create_conversation(
            target_id,
            owner_id=owner_id,
            # Keep the user-facing task in Agent mode.  Artifact mode and the
            # deck contract below—not the navigation surface—seal execution to
            # the first-class deck path.
            surface="agent",
            title=f"Deck: {job.goal[:80]}",
        )
        self._runtime.settings.set_artifact_mode(target_id, True)
        self._runtime.contract.set_build_kind(target_id, "deck")
        await self._persist_source(target_id, source_conversation_id, job)
        # Persist the typed pending fact before admitting any async work.  A
        # restart can reproject this marker through the normal run supervisor.
        await self._store.append(
            target_id,
            ReportDeckInvocationEvent(
                source_conversation_id=source_conversation_id,
                goal=job.goal,
                filename=job.filename,
                format=job.format,
                status="queued",
            ),
        )
        await self._schedule_tool(
            target_id,
            job,
            ToolCall(
                tool_name="slides_generate",
                arguments={
                    "goal": job.goal,
                    "filename": job.filename,
                    "format": job.format,
                },
            ),
        )
        return target_id


class ReportDeckStartResponse(BaseModel):
    ok: bool = True
    conversation_id: str
    job_id: str
    source_event_id: str
    contract: str = "deck"
    format: str = "pptx"


def latest_report(events: Sequence[object]) -> ReportEvent | None:
    """Resolve the latest finished report from one authoritative event horizon."""
    return next((event for event in reversed(events) if isinstance(event, ReportEvent)), None)


def make_report_decks_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    start_port: ReportDeckStartPort | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/conversations/{conversation_id}/report/deck",
        response_model=ReportDeckStartResponse,
        status_code=202,
    )
    async def start_report_deck(
        conversation_id: str,
        request: Request,
    ) -> ReportDeckStartResponse:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        events = await store.get_events(conversation_id)
        state = ConversationState.reconstruct(conversation_id, events)
        if state.execution_status is not ConversationStatus.FINISHED:
            raise HTTPException(status_code=409, detail={"reason": "report_not_finished"})
        report = latest_report(events)
        if report is None:
            raise HTTPException(status_code=404, detail={"reason": "no_report"})
        if start_port is None:
            raise HTTPException(status_code=503, detail={"reason": "handoff_not_wired"})

        job = build_report_deck_job(report)
        target_id = await start_port.start_report_deck(conversation_id, job)
        return ReportDeckStartResponse(
            conversation_id=target_id,
            job_id=target_id,
            source_event_id=report.id,
        )

    return router


__all__ = [
    "DirectReportDeckStartPort",
    "ReportDeckStartPort",
    "ReportDeckStartResponse",
    "latest_report",
    "make_report_decks_router",
]

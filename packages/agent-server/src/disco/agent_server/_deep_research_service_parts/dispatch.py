"""Deep Research phase-dispatch predicates.

Extracted from ``DeepResearchService._maybe_run_deep_research``
(PKG-11-RETRIEVAL wave 1, PY-0194): the phase-detection *decisions* live here
as small, named predicates so the dispatcher itself reads as a near-linear
sequence of guard clauses instead of one 31-branch function. Moving branches
into these predicates is what actually reduces the dispatcher's cyclomatic
complexity — relocating the whole blob into one function elsewhere would not
have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import (
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    PlanEvent,
    ReportEvent,
    StatusEvent,
)

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService


def latest_status(events: list[Event]) -> StatusEvent | None:
    """The most recent StatusEvent in the log, or None."""
    return next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)


def _has_fresh_message_since(events: list[Event], seq: int) -> bool:
    """True when a non-empty USER message arrived after ``seq``."""
    return any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and (e.seq or 0) > seq
        and (e.message.content or "").strip()
        for e in events
    )


def has_revision_signal(
    events: list[Event], state: ConversationState, plans: list[PlanEvent]
) -> bool:
    """Phase 1R (live STUCK 2026-07-07): a plan exists but is NOT approved and
    the user sent revision text. Two ingress shapes:
      * the PlanPanel "Send revision" -> request_plan -> loop.enter_planning
        emits RUNNING/'planning' (the status this checks for);
      * a plain message typed while AWAITING_PLAN_APPROVAL (fresh user
        message newer than the latest plan).
    Only called when ``plans`` is already known non-empty (Phase 1 already
    handled the empty case with an early return)."""
    last_status = latest_status(events)
    in_revision_planning = (
        state.execution_status == ConversationStatus.RUNNING
        and last_status is not None
        and last_status.detail == "planning"
    )
    if in_revision_planning:
        return True
    latest_plan_seq = plans[-1].seq or 0
    return (
        state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
        and _has_fresh_message_since(events, latest_plan_seq)
    )


def is_plan_just_approved(events: list[Event]) -> bool:
    """True when the last StatusEvent's detail is 'plan_approved' — the
    marker distinguishing 'RUNNING because plan was just approved' from
    'RUNNING because we're already deep in the engine and the task
    re-fired.'"""
    last_status = latest_status(events)
    return last_status is not None and last_status.detail == "plan_approved"


def _split_plans_and_reports(
    events: list[Event],
) -> tuple[list[PlanEvent], list[ReportEvent]]:
    plans = [e for e in events if isinstance(e, PlanEvent)]
    reports = [e for e in events if isinstance(e, ReportEvent)]
    return plans, reports


async def run_phase(service: DeepResearchService, conversation_id: str) -> None:
    """The Deep Research driver. Inspects the conversation state to decide
    what to do this turn:
    - No PlanEvent yet + a USER message -> decompose + emit synthetic
      PlanEvent + AWAITING_PLAN_APPROVAL. Wait for the user to approve.
    - A plan exists but is unapproved and the user sent revision text ->
      re-propose (Phase 1R, live STUCK 2026-07-07).
    - A stopped run (PAUSED) -> resume from the checkpoint (Phase 2b).
    - PlanEvent exists + status is RUNNING with detail="plan_approved" +
      no ReportEvent yet -> run the engine, emit progress events, emit
      ReportEvent + StatusEvent(FINISHED) (Phase 2).
    - A FINISHED report exists + a fresh user message -> follow-up
      synthesis reusing the report's passages as grounding (Phase 3, RP-13).
    - Anything else -> no-op (waiting on the user, or already finished).

    All actions persist via the event store; the WS surface streams them.
    Failures surface as ErrorEvent on the log — never raise out of the
    background task. Every call below is on ``service`` (an instance
    attribute lookup), so instance-level test monkeypatches
    (``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``) keep
    resolving correctly regardless of this module boundary."""
    events = await service._store.get_events(conversation_id)
    state = await service._store.get_state(conversation_id)
    plans, reports = _split_plans_and_reports(events)

    # Phase 1: no plan yet → decompose + propose
    if not plans:
        await service._propose_deep_research_plan(conversation_id, events)
        return

    # Phase 1R: plan revision — see has_revision_signal for the ingress shapes.
    if not reports and has_revision_signal(events, state, plans):
        await service._propose_deep_research_plan(conversation_id, events)
        return

    # Phase 2b: RESUME a stopped run (status PAUSED) → continue from the
    # checkpoint. The plan is already approved; flip back to RUNNING and
    # execute, carrying the partial ReportEvent's completed sections so the
    # engine skips the sub-questions that already finished.
    if state.execution_status == ConversationStatus.PAUSED:
        partial = reports[-1] if reports else None
        await service._lifecycle_commands.append_status(
            conversation_id,
            ConversationStatus.RUNNING,
            detail="plan_approved",
        )
        await service._execute_deep_research(conversation_id, plans[-1], resume_from=partial)
        return

    # Phase 2: plan approved, no report yet → run the engine
    if state.execution_status == ConversationStatus.RUNNING and not reports:
        if is_plan_just_approved(events):
            await service._execute_deep_research(conversation_id, plans[-1])
        return

    # Phase 3: FOLLOW-UP — a FINISHED report exists AND there's a new user
    # message since the last report (RP-13).
    if reports and service._has_fresh_user_message(events, reports):
        await service._follow_up_deep_research(conversation_id, events, reports[-1])

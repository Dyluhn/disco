"""Deep Research phase dispatch.

v2 (PKG-35, decision #7): there is NO plan gate. The old propose-plan →
AWAITING_PLAN_APPROVAL → approve → execute pipeline (and its revision
re-propose) is gone for this surface — the first user message IS the launch.
The dispatcher's job is now only to pick between kickoff, resume, follow-up,
and no-op. The Build/Agent plan machinery is untouched; this module simply no
longer routes Deep Research through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
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


async def run_phase(service: DeepResearchService, conversation_id: str) -> None:
    """The Deep Research driver (v2 — gateless, decision #7). Inspects the
    conversation state to decide what to do this turn:
    - A stopped run (PAUSED) -> resume from the checkpoint, carrying the
      partial ReportEvent's evidence + issued queries forward.
    - A report exists + a fresh user message -> follow-up synthesis reusing
      the report's passages as grounding (RP-13).
    - No report + status RUNNING -> no-op: the engine is executing (user
      input mid-run routes into the WS steer queue, never a second run) or a
      crashed run left the status stale (Stop/resume owns recovery).
    - No report + a user question -> KICKOFF: execute immediately. No
      PlanEvent proposal, no AWAITING_PLAN_APPROVAL, no approval step — the
      model's streamed brief is its first visible output. An errored run
      re-kicks fresh, but only on NEW user input (never from a bare re-kick,
      which would loop an erroring run).
    - Anything else -> no-op (waiting on the user, or already finished).

    All actions persist via the event store; the WS surface streams them.
    Failures surface as ErrorEvent on the log — never raise out of the
    background task. Every call below is on ``service`` (an instance
    attribute lookup), so instance-level test monkeypatches
    (``monkeypatch.setattr(rt.deep_research, "_execute_deep_research", ...)``) keep
    resolving correctly regardless of this module boundary."""
    from .execute import build_query_with_constraints

    events = await service._store.get_events(conversation_id)
    state = await service._store.get_state(conversation_id)
    reports = [e for e in events if isinstance(e, ReportEvent)]

    # Phase R: RESUME a stopped run (status PAUSED) → continue from the
    # checkpoint. The prior partial ReportEvent carries the gathered evidence
    # and the queries already issued, so the engine doesn't redo them.
    if state.execution_status == ConversationStatus.PAUSED:
        await service._execute_deep_research(
            conversation_id, resume_from=reports[-1] if reports else None
        )
        return

    # Phase F: FOLLOW-UP — a finished report exists AND there's a new user
    # message since the last report (RP-13). A report without a fresh
    # message is a finished conversation: no-op.
    if reports:
        if service._has_fresh_user_message(events, reports):
            await service._follow_up_deep_research(conversation_id, events, reports[-1])
        return

    # In-flight guard: RUNNING with no report means the engine is executing
    # this conversation (mid-run user input routes into the steer queue over
    # the WS — see routes/ws._handle_steer_frame — never a second run), or a
    # crashed run left the status stale (Stop/resume owns that recovery).
    if state.execution_status == ConversationStatus.RUNNING:
        return

    # An errored run retries fresh on NEW user input only. A bare re-kick
    # (schedule, reconnect, sweep) must never restart an erroring run.
    last_status = latest_status(events)
    if (
        last_status is not None
        and last_status.status == ConversationStatus.ERROR
        and not _has_fresh_message_since(events, last_status.seq or 0)
    ):
        return

    # Phase K: KICKOFF (decision #7 — no plan gate). The first user message
    # is the research question; execution starts immediately.
    if build_query_with_constraints(events) is None:
        return  # nothing to research yet; wait for the user
    await service._execute_deep_research(conversation_id)

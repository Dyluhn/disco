"""State reconstruction — event-state-contract.md §3.

`ConversationState` is a *derived* projection of the event log, never the
source of truth. `reconstruct()` is pure (invariant #3): identical ordered
event lists yield identical state, every time, with no hidden state. The store
rebuilds this on load by replaying events; it may cache/incrementally update
the projection as long as it equals a full reconstruct().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, Field

from .events import (
    ActionEvent,
    AlternativesEvent,
    ClarifyEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    PlanEvent,
    QuestionsV2Event,
    StatusEvent,
    WorkspaceMutationEvent,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    current_workspace_agent_view_seq,
    workspace_run_intent_admission_required,
)


@dataclass
class _ReplayCursor:
    run_iteration: int = 0
    last_action_id: str | None = None
    last_plan_id: str | None = None
    last_alternatives_id: str | None = None
    last_agent_message_id: str | None = None
    last_clarify_id: str | None = None
    last_questions_v2_id: str | None = None


def _clear_pending_gates(state: ConversationState) -> None:
    state.pending_action_id = None
    state.pending_plan_id = None
    state.pending_alternatives_id = None
    state.pending_question_id = None
    state.pending_clarify_id = None
    state.pending_questions_v2_id = None


def _workspace_transition_reopens(event: Event) -> bool:
    if not isinstance(event, WorkspaceMutationEvent):
        return False
    if event.run_protocol_version != 1:
        return False
    return event.operation.startswith("agent.run-intent.") or (
        event.operation == "agent.view-admitted"
    )


def _apply_status(
    state: ConversationState,
    event: StatusEvent,
    cursor: _ReplayCursor,
) -> None:
    state.execution_status = event.status
    state.pending_action_id = (
        cursor.last_action_id
        if event.status == ConversationStatus.WAITING_FOR_CONFIRMATION
        else None
    )
    state.pending_plan_id = (
        cursor.last_plan_id if event.status == ConversationStatus.AWAITING_PLAN_APPROVAL else None
    )
    state.pending_alternatives_id = (
        cursor.last_alternatives_id
        if event.status == ConversationStatus.AWAITING_USER_DECISION
        else None
    )
    if event.status != ConversationStatus.AWAITING_USER_QUESTION:
        state.pending_question_id = None
        state.pending_clarify_id = None
        state.pending_questions_v2_id = None
        return
    state.pending_question_id = event.detail or cursor.last_agent_message_id
    state.pending_clarify_id = (
        cursor.last_clarify_id if event.detail == cursor.last_clarify_id else None
    )
    state.pending_questions_v2_id = (
        cursor.last_questions_v2_id if event.detail == cursor.last_questions_v2_id else None
    )


def _apply_replay_event(
    state: ConversationState,
    event: Event,
    cursor: _ReplayCursor,
) -> None:
    if _workspace_transition_reopens(event):
        state.execution_status = ConversationStatus.RUNNING
        _clear_pending_gates(state)
    elif isinstance(event, StatusEvent):
        _apply_status(state, event, cursor)
    elif isinstance(event, ActionEvent):
        cursor.run_iteration += 1
        cursor.last_action_id = event.id
    elif isinstance(event, PlanEvent):
        cursor.last_plan_id = event.id
    elif isinstance(event, AlternativesEvent):
        cursor.last_alternatives_id = event.id
    elif isinstance(event, MessageEvent):
        if event.source == EventSource.USER:
            cursor.run_iteration = 0
        elif event.source == EventSource.AGENT:
            cursor.last_agent_message_id = event.id
    elif isinstance(event, ClarifyEvent):
        cursor.last_clarify_id = event.id
    elif isinstance(event, QuestionsV2Event):
        cursor.last_questions_v2_id = event.id
    elif isinstance(event, ErrorEvent):
        state.execution_status = ConversationStatus.ERROR


class ConversationState(BaseModel):
    """Derived, in-memory projection of the event log. NOT the source of truth.

    [CONTRACT] reconstruct(events) is pure. The loop reads execution_status from
    here; the store rebuilds this on load by replaying events.
    """

    conversation_id: str
    execution_status: ConversationStatus = ConversationStatus.IDLE
    iteration: int = 0  # count of ActionEvents in the current run
    max_iterations: int = 500  # hard ceiling (BoD §12.1)
    last_seq: int = 0  # highest seq observed
    active_agent_view_id: str | None = None
    active_agent_view_seq: int | None = None
    agent_view_pending: bool = False
    # The id of an action awaiting confirmation, if status is
    # WAITING_FOR_CONFIRMATION. Enables the two-phase confirm step (BoD §12.4).
    pending_action_id: str | None = None
    # The id of a plan awaiting approval, if status is AWAITING_PLAN_APPROVAL.
    # Mirror of pending_action_id for the plan-mode gate (Build).
    pending_plan_id: str | None = None
    # The id of an AlternativesEvent awaiting a user pick, if status is
    # AWAITING_USER_DECISION. The third gate (with pending_action_id and
    # pending_plan_id) — same idempotency model.
    pending_alternatives_id: str | None = None
    # The id of the agent's free-form question MessageEvent awaiting a typed
    # answer, if status is AWAITING_USER_QUESTION. The two-way Ask-gate's pending
    # id — lets a reconnecting surface resolve the question from a state snapshot.
    pending_question_id: str | None = None
    # The id of a ClarifyEvent awaiting answers, if status is
    # AWAITING_USER_QUESTION and the gate is a clarify card (not a free-form
    # ask_user question). The frontend resolves this to render ClarifyPanel
    # instead of AskPanel.
    pending_clarify_id: str | None = None
    # The id of a QuestionsV2Event awaiting answers, if status is
    # AWAITING_USER_QUESTION and the gate is a structured intake form. This is
    # distinct from legacy clarify so clients can render the richer v2 form.
    pending_questions_v2_id: str | None = None
    # Feature-scoped scratch state; keys are namespaced by subsystem,
    # e.g. "memory.last_condense_seq". [CONTRACT]
    extras: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def reconstruct(
        cls,
        conversation_id: str,
        events: list[Event],
        max_iterations: int = 500,
    ) -> ConversationState:
        st = cls(conversation_id=conversation_id, max_iterations=max_iterations)
        st.last_seq = max(
            (event.seq for event in events if type(event.seq) is int),
            default=0,
        )
        st.active_agent_view_id = current_workspace_agent_view_id(events)
        st.active_agent_view_seq = current_workspace_agent_view_seq(events)
        st.agent_view_pending = workspace_run_intent_admission_required(events)
        events = cast(list[Event], agent_view_consistent_events(events))
        cursor = _ReplayCursor()
        for event in events:
            _apply_replay_event(st, event, cursor)
        st.iteration = cursor.run_iteration
        return st

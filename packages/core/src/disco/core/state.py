"""State reconstruction — event-state-contract.md §3.

`ConversationState` is a *derived* projection of the event log, never the
source of truth. `reconstruct()` is pure (invariant #3): identical ordered
event lists yield identical state, every time, with no hidden state. The store
rebuilds this on load by replaying events; it may cache/incrementally update
the projection as long as it equals a full reconstruct().
"""

from __future__ import annotations

from typing import Any

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
)


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
        run_iteration = 0
        # The most recent action's id — the candidate awaiting confirmation when
        # the loop transitions to WAITING_FOR_CONFIRMATION (two-phase confirm).
        last_action_id: str | None = None
        # The most recent plan's id — the candidate awaiting approval when the
        # loop transitions to AWAITING_PLAN_APPROVAL (the plan-mode gate).
        last_plan_id: str | None = None
        # Likewise for the alternatives gate (AWAITING_USER_DECISION).
        last_alternatives_id: str | None = None
        # The most recent agent message id — the candidate question when the loop
        # transitions to AWAITING_USER_QUESTION (the free-form Ask-gate).
        last_agent_message_id: str | None = None
        # The most recent ClarifyEvent id — the candidate when the loop
        # transitions to AWAITING_USER_QUESTION via the clarify gate.
        last_clarify_id: str | None = None
        # The most recent QuestionsV2Event id — the candidate when the loop
        # transitions to AWAITING_USER_QUESTION via the questions_v2 gate.
        last_questions_v2_id: str | None = None

        for e in events:
            if e.seq is not None:
                st.last_seq = e.seq

            if isinstance(e, StatusEvent):
                st.execution_status = e.status
                if e.status == ConversationStatus.WAITING_FOR_CONFIRMATION:
                    # The pending action is the latest one the agent proposed.
                    st.pending_action_id = last_action_id
                else:
                    st.pending_action_id = None
                if e.status == ConversationStatus.AWAITING_PLAN_APPROVAL:
                    st.pending_plan_id = last_plan_id
                else:
                    st.pending_plan_id = None
                if e.status == ConversationStatus.AWAITING_USER_DECISION:
                    st.pending_alternatives_id = last_alternatives_id
                else:
                    st.pending_alternatives_id = None
                if e.status == ConversationStatus.AWAITING_USER_QUESTION:
                    # Prefer the explicit id the engine stamped on the status
                    # event; fall back to the last agent message.
                    st.pending_question_id = e.detail or last_agent_message_id
                    # This gate is a clarify card ONLY when the engine stamped THIS
                    # status with the clarify event's id. A free-form ask_user gate
                    # stamps the agent message id instead — so gating on the detail
                    # (not just "the last clarify seen") stops a stale clarify card
                    # from shadowing a later ask_user question on reload/reconnect.
                    st.pending_clarify_id = (
                        last_clarify_id if e.detail == last_clarify_id else None
                    )
                    st.pending_questions_v2_id = (
                        last_questions_v2_id if e.detail == last_questions_v2_id else None
                    )
                else:
                    st.pending_question_id = None
                    st.pending_clarify_id = None
                    st.pending_questions_v2_id = None
            elif isinstance(e, ActionEvent):
                run_iteration += 1
                last_action_id = e.id
            elif isinstance(e, PlanEvent):
                last_plan_id = e.id
            elif isinstance(e, AlternativesEvent):
                last_alternatives_id = e.id
            elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
                # A fresh user instruction starts a new run (ceiling + stuck).
                run_iteration = 0
            elif isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
                # Track the latest agent message — the free-form Ask-gate's
                # question is the agent message just before the status flip.
                last_agent_message_id = e.id
            elif isinstance(e, ClarifyEvent):
                last_clarify_id = e.id
            elif isinstance(e, QuestionsV2Event):
                last_questions_v2_id = e.id
            elif isinstance(e, ErrorEvent):
                st.execution_status = ConversationStatus.ERROR

        st.iteration = run_iteration
        return st

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
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    PlanEvent,
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
            elif isinstance(e, ActionEvent):
                run_iteration += 1
                last_action_id = e.id
            elif isinstance(e, PlanEvent):
                last_plan_id = e.id
            elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
                # A fresh user instruction starts a new run (ceiling + stuck).
                run_iteration = 0
            elif isinstance(e, ErrorEvent):
                st.execution_status = ConversationStatus.ERROR

        st.iteration = run_iteration
        return st

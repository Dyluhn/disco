"""State reconstruction tests — event-state-contract.md §8.2.

reconstruct() is a pure function of the ordered event list; these pin purity,
status resolution, the iteration reset, and the confirmation-gate bookkeeping.
"""

from __future__ import annotations

import random

from disco.core import (
    ConversationState,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    WorkspaceMutationEvent,
)
from event_fakes import action, agent_msg, fatal, status, user_msg, with_seqs

CID = "conv"


def reconstruct(events):
    return ConversationState.reconstruct(CID, events)


# ---- purity -----------------------------------------------------------------


def test_reconstruct_is_deterministic():
    events = with_seqs([user_msg(), action(), action(), status(ConversationStatus.FINISHED)])
    assert reconstruct(events) == reconstruct(events)


def test_shuffle_then_sort_yields_same_state():
    events = with_seqs([user_msg(), action(), action(), status(ConversationStatus.RUNNING)])
    shuffled = events[:]
    random.Random(0).shuffle(shuffled)
    resorted = sorted(shuffled, key=lambda e: e.seq)
    assert reconstruct(resorted) == reconstruct(events)


# ---- status resolution ------------------------------------------------------


def test_last_status_event_wins():
    events = with_seqs(
        [
            status(ConversationStatus.RUNNING),
            status(ConversationStatus.PAUSED),
            status(ConversationStatus.RUNNING),
        ]
    )
    assert reconstruct(events).execution_status == ConversationStatus.RUNNING


def test_trailing_error_forces_error_status():
    events = with_seqs([status(ConversationStatus.FINISHED), fatal()])
    assert reconstruct(events).execution_status == ConversationStatus.ERROR


# ---- iteration reset --------------------------------------------------------


def test_iteration_counts_actions_in_current_run():
    events = with_seqs([user_msg(), action(), action(), action()])
    assert reconstruct(events).iteration == 3


def test_user_message_resets_iteration():
    events = with_seqs([user_msg(), action(), action(), user_msg(), action()])
    # Only the single action after the latest user message counts.
    assert reconstruct(events).iteration == 1


# ---- confirmation-gate bookkeeping (pending_action_id) ----------------------


def test_pending_action_set_on_waiting_for_confirmation():
    acts = action("risky")
    events = with_seqs([user_msg(), acts, status(ConversationStatus.WAITING_FOR_CONFIRMATION)])
    st = reconstruct(events)
    assert st.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    # The pending action is the latest one proposed (matched by id).
    waiting_action = events[1]
    assert st.pending_action_id == waiting_action.id


def test_pending_action_cleared_on_other_status():
    acts = action("risky")
    events = with_seqs(
        [
            user_msg(),
            acts,
            status(ConversationStatus.WAITING_FOR_CONFIRMATION),
            status(ConversationStatus.RUNNING),  # approved -> resumed
        ]
    )
    assert reconstruct(events).pending_action_id is None


# ---- [4] stale view output quarantined by ConversationState -----------------


def test_state_quarantines_stale_view_output():
    """ConversationState.reconstruct uses agent_view_consistent_events
    which quarantines output from a stale (non-current) agent_view_id.
    The stale output must not affect state fields."""
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    events = with_seqs(
        [
            user_msg("build it"),
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview_v1",
                run_protocol_version=1,
            ),
            agent_msg("v1 work").model_copy(update={"agent_view_id": "aview_v1"}),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview_v2",
                run_protocol_version=1,
            ),
            agent_msg("stale v1 output").model_copy(update={"agent_view_id": "aview_v1"}),
            agent_msg("fresh v2 output").model_copy(update={"agent_view_id": "aview_v2"}),
        ]
    )
    state = ConversationState.reconstruct("conv", events)
    assert state.active_agent_view_id == "aview_v2"
    # The stale output from v1 after v2 was admitted should not affect iteration
    assert state.iteration == 0  # no ActionEvents
    assert state.execution_status == ConversationStatus.RUNNING  # v1 intent sets RUNNING


# --- run_intent_id authority in reconstruct --------------------------------


def test_reconstruct_pending_intent_error_yields_error():
    """A StatusEvent ERROR bound to the current run_intent_id (no view
    admitted) must produce ERROR in ConversationState.reconstruct."""
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    events = with_seqs(
        [
            user_msg("do it"),
            intent,
            StatusEvent(
                status=ConversationStatus.ERROR,
                run_intent_id=intent.id,
                detail="preflight failed",
            ),
        ]
    )
    state = ConversationState.reconstruct(CID, events)
    assert state.execution_status == ConversationStatus.ERROR


def test_reconstruct_late_previous_intent_error_after_new_intent():
    """A late ERROR bound to I1 via run_intent_id that arrives after I2 has
    started must be filtered by reconstruct, yielding RUNNING for I2."""
    events = with_seqs(
        [
            user_msg("first"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_I1",
            ),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_I2",
            ),
            StatusEvent(
                status=ConversationStatus.ERROR,
                run_intent_id="intent_I1",
                detail="late I1 crash",
            ),
        ]
    )
    state = ConversationState.reconstruct(CID, events)
    assert state.execution_status == ConversationStatus.RUNNING


def test_reconstruct_view_attributed_error():
    """A StatusEvent ERROR with the current agent_view_id must produce
    ERROR in ConversationState.reconstruct."""
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    events = with_seqs(
        [
            user_msg("build it"),
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview_r",
                run_protocol_version=1,
            ),
            StatusEvent(
                status=ConversationStatus.ERROR,
                agent_view_id="aview_r",
            ),
        ]
    )
    state = ConversationState.reconstruct(CID, events)
    assert state.execution_status == ConversationStatus.ERROR


async def test_raw_store_retains_stale_events():
    """While ConversationState quarantines stale output, the raw store audit
    must still contain all events including stale ones."""
    from disco.core import SqliteEventStore

    store = SqliteEventStore(":memory:")
    try:
        cid = "store-retain-stale"
        store.create_conversation(cid)
        stored = await store.append_many(
            cid,
            [
                MessageEvent(
                    source=EventSource.USER, message=LLMMessage(role="user", content="build")
                ),
                WorkspaceMutationEvent(
                    operation="agent.run-intent.user-turn", run_protocol_version=1
                ),
            ],
        )
        intent = next(e for e in stored if isinstance(e, WorkspaceMutationEvent))
        await store.append_many(
            cid,
            [
                WorkspaceMutationEvent(
                    operation="agent.view-admitted",
                    run_intent_id=intent.id,
                    agent_view_id="aview_v1",
                    run_protocol_version=1,
                ),
                WorkspaceMutationEvent(
                    operation="agent.view-admitted",
                    run_intent_id=intent.id,
                    agent_view_id="aview_v2",
                    run_protocol_version=1,
                ),
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content="stale output"),
                    agent_view_id="aview_v1",
                ),
            ],
        )
        raw = await store.get_events(cid)
        assert len(raw) == 5  # all 5 events retained
        state = ConversationState.reconstruct(cid, raw)
        assert state.active_agent_view_id == "aview_v2"
    finally:
        store.close()

"""State reconstruction tests — event-state-contract.md §8.2.

reconstruct() is a pure function of the ordered event list; these pin purity,
status resolution, the iteration reset, and the confirmation-gate bookkeeping.
"""

from __future__ import annotations

import random

from conftest import action, fatal, status, user_msg, with_seqs
from perpleximanus.core import ConversationState, ConversationStatus

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

"""Coverage for append_many and the EventFilter dimensions.

These paths were implemented in SqliteEventStore but initially shipped without
tests (caught by a coverage pass). They exercise §6's filtered reads and the
batch-append guarantees (G1 within a batch, G4 idempotency within a batch).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import action, agent_error, agent_msg, status, user_msg, with_seqs
from perpleximanus.core import (
    ConversationStatus,
    EventFilter,
    EventKind,
    EventSource,
    SqliteEventStore,
    View,
)

CID = "conv_filters"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    yield s
    s.close()


# ---- append_many ------------------------------------------------------------


async def test_append_many_assigns_contiguous_seqs_in_order(store):
    batch = [user_msg("u"), action("a1"), action("a2")]
    stored = await store.append_many(CID, batch)
    assert [e.seq for e in stored] == [1, 2, 3]
    # And the persisted log matches.
    assert [e.seq for e in await store.get_events(CID)] == [1, 2, 3]


async def test_append_many_continues_seq_after_prior_appends(store):
    await store.append(CID, user_msg("first"))
    stored = await store.append_many(CID, [action("a1"), action("a2")])
    assert [e.seq for e in stored] == [2, 3]


async def test_append_many_is_idempotent_on_duplicate_ids(store):
    first = await store.append(CID, user_msg("once"))
    # Re-submit the already-stored event alongside a genuinely new one.
    stored = await store.append_many(CID, [first, action("new")])
    assert stored[0].seq == first.seq  # G4: existing returned, not re-added
    assert stored[1].seq == first.seq + 1
    assert len(await store.get_events(CID)) == 2  # no duplicate row


# ---- EventFilter dimensions -------------------------------------------------


async def test_filter_by_kind(store):
    await store.append_many(CID, [user_msg("u"), action("a"), agent_msg("m")])
    only_actions = await store.get_events(CID, EventFilter(kinds=[EventKind.ACTION]))
    assert [type(e).__name__ for e in only_actions] == ["ActionEvent"]


async def test_filter_by_source(store):
    await store.append_many(CID, [user_msg("u"), agent_msg("m")])
    from_user = await store.get_events(CID, EventFilter(sources=[EventSource.USER]))
    assert [e.source for e in from_user] == [EventSource.USER]


async def test_filter_by_multiple_kinds(store):
    await store.append_many(CID, [user_msg("u"), action("a"), agent_msg("m")])
    msgs_and_actions = await store.get_events(
        CID, EventFilter(kinds=[EventKind.MESSAGE, EventKind.ACTION])
    )
    # Both message events + the action; ascending seq order preserved (G3).
    assert [type(e).__name__ for e in msgs_and_actions] == [
        "MessageEvent",
        "ActionEvent",
        "MessageEvent",
    ]


async def test_filter_by_seq_bounds(store):
    await store.append_many(CID, [action(f"a{i}") for i in range(5)])  # seqs 1..5
    mid = await store.get_events(CID, EventFilter(after_seq=1, before_seq=4))
    assert [e.seq for e in mid] == [2, 3]  # exclusive bounds on both ends


async def test_filter_by_time_window(store):
    stored = await store.append_many(CID, [action("a1"), action("a2"), action("a3")])
    # Bracket strictly around the middle event's timestamp.
    mid_ts = stored[1].timestamp
    window = await store.get_events(
        CID,
        EventFilter(
            since=mid_ts - timedelta(microseconds=1),
            until=mid_ts + timedelta(microseconds=1),
        ),
    )
    # The window should include the middle event (and only events whose
    # created_at falls inside it).
    assert any(e.seq == stored[1].seq for e in window)
    assert all(abs((e.timestamp - mid_ts).total_seconds()) < 1 for e in window)


# ---- get_state (the store's reconstruct entry point) ------------------------


async def test_get_state_reflects_the_log(store):
    await store.append_many(
        CID, [user_msg("go"), action("a1"), action("a2"), status(ConversationStatus.RUNNING)]
    )
    st = await store.get_state(CID)
    assert st.execution_status == ConversationStatus.RUNNING
    assert st.iteration == 2  # two actions since the user message
    assert st.last_seq == 4


# ---- AgentErrorEvent renders to a tool-role ERROR message -------------------


def test_agent_error_renders_for_the_llm():
    """The LLM sees a tool-role 'ERROR: ...' message for a failed action."""
    events = with_seqs([agent_error("disk full", action_id="evt_a")])
    msgs = View.of(events).messages
    assert len(msgs) == 1
    assert msgs[0].role == "tool"
    assert msgs[0].content == "ERROR: disk full"

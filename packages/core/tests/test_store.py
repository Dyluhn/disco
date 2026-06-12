"""EventStore invariant tests — event-state-contract.md §8.1 (+ subscribe).

These prove the load-bearing storage guarantees G1 (monotonic gap-free seq),
G2 (atomic/durable), G4 (idempotency), and append-only-ness, plus the §7
reconnect/replay subscribe path.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import action, user_msg
from disco.core import SqliteEventStore
from pydantic import ValidationError

CID = "conv_test"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    yield s
    s.close()


# ---- append-only ------------------------------------------------------------


def test_events_are_frozen():
    """Mutating a persisted event raises (frozen model) — invariant #1."""
    e = user_msg()
    with pytest.raises(ValidationError):
        e.seq = 99  # type: ignore[misc]


def test_store_exposes_no_update_or_delete():
    """The store offers no mutate/delete surface; forgetting is via tombstones."""
    for forbidden in ("update", "delete", "update_event", "delete_event", "remove"):
        assert not hasattr(SqliteEventStore, forbidden)


# ---- G1: monotonic, gap-free seq under concurrency --------------------------


async def test_g1_monotonic_seq_under_concurrency(store):
    """N concurrent appends to one conversation yield seqs 1..N, no gaps/dups."""
    n = 50
    results = await asyncio.gather(*(store.append(CID, action(thought=f"a{i}")) for i in range(n)))
    seqs = sorted(e.seq for e in results)
    assert seqs == list(range(1, n + 1))


async def test_g1_seq_assigned_on_return(store):
    """append() returns the event with seq populated; the input had none."""
    e = user_msg()
    assert e.seq is None
    stored = await store.append(CID, e)
    assert stored.seq == 1


# ---- G4: idempotency --------------------------------------------------------


async def test_g4_idempotent_append(store):
    """Appending the same event id twice is a no-op returning the existing
    event; log length unchanged."""
    e = await store.append(CID, user_msg("once"))
    again = await store.append(CID, e)  # same id (and now seq)
    assert again.seq == e.seq
    events = await store.get_events(CID)
    assert len(events) == 1


# ---- G2: atomicity / durability across reopen -------------------------------


async def test_g2_durable_across_reopen(tmp_path):
    """An append that completes is present after reopening the store file."""
    db = tmp_path / "events.db"
    s1 = SqliteEventStore(db)
    await s1.append(CID, user_msg("persist me"))
    await s1.append(CID, action("then act"))
    s1.close()

    s2 = SqliteEventStore(db)
    events = await s2.get_events(CID)
    assert [e.seq for e in events] == [1, 2]
    s2.close()


# ---- G3 + reads -------------------------------------------------------------


async def test_get_events_ascending_and_gap_free(store):
    for i in range(5):
        await store.append(CID, action(thought=f"a{i}"))
    events = await store.get_events(CID)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


async def test_pagination_cursor(store):
    for i in range(10):
        await store.append(CID, action(thought=f"a{i}"))
    page1 = await store.paginate(CID, limit=4)
    assert [e.seq for e in page1.events] == [1, 2, 3, 4]
    assert page1.next_cursor == 4
    page2 = await store.paginate(CID, after_seq=page1.next_cursor, limit=4)
    assert [e.seq for e in page2.events] == [5, 6, 7, 8]
    page3 = await store.paginate(CID, after_seq=page2.next_cursor, limit=4)
    assert [e.seq for e in page3.events] == [9, 10]
    assert page3.next_cursor is None  # end reached


# ---- §6.1 ownership door ----------------------------------------------------


async def test_list_conversations_is_owner_scoped(store):
    store.create_conversation("c_alice", owner_id="alice")
    store.create_conversation("c_bob", owner_id="bob")
    await store.append("c_alice", user_msg())
    await store.append("c_bob", user_msg())
    assert await store.list_conversations(owner_id="alice") == ["c_alice"]
    assert await store.list_conversations(owner_id="bob") == ["c_bob"]
    # No query ever returns cross-owner data (§6.1).
    assert "c_bob" not in await store.list_conversations(owner_id="alice")


async def test_conversation_exists(store):
    assert not await store.conversation_exists(CID)
    await store.append(CID, user_msg())
    assert await store.conversation_exists(CID)


# ---- subscribe: history-then-live, overlap deduped (§6.2 / §7) --------------


async def test_subscribe_drains_history_then_streams_live(store):
    await store.append(CID, action(thought="historic-1"))
    await store.append(CID, action(thought="historic-2"))

    received: list[int] = []
    gen = await store.subscribe(CID, after_seq=0)

    async def consume():
        async for ev in gen:
            received.append(ev.seq)
            if len(received) == 3:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the consumer drain history + register
    await store.append(CID, action(thought="live-3"))
    await asyncio.wait_for(task, timeout=2)

    assert received == [1, 2, 3]  # history (1,2) then live (3), no dup/gap

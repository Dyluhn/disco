"""UI-31: `update_title_unique` — the store-side claim that keeps two conversations
from carrying the identical History/Projects label.

Owner-scoped (the scope `list_conversation_summaries` lists in) and race-tolerant
(the read of the taken names and the write of the winner share one transaction)."""

from __future__ import annotations

import asyncio

import pytest
from disco.core import SqliteEventStore

TITLE = "Coffee Shop Subscription Website"


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    yield s
    s.close()


async def test_second_and_third_claim_of_a_title_get_a_counter(store: SqliteEventStore) -> None:
    for cid in ("c1", "c2", "c3"):
        store.create_conversation(cid, owner_id="local")
    assert await store.update_title_unique("c1", TITLE) == TITLE
    assert await store.update_title_unique("c2", TITLE) == f"{TITLE} (2)"
    assert await store.update_title_unique("c3", TITLE) == f"{TITLE} (3)"
    assert await store.get_title("c3") == f"{TITLE} (3)"


async def test_concurrent_claims_do_not_collide(store: SqliteEventStore) -> None:
    for cid in ("c1", "c2"):
        store.create_conversation(cid, owner_id="local")
    claimed = await asyncio.gather(
        store.update_title_unique("c1", TITLE),
        store.update_title_unique("c2", TITLE),
    )
    assert set(claimed) == {TITLE, f"{TITLE} (2)"}


async def test_collision_scope_is_the_owner(store: SqliteEventStore) -> None:
    store.create_conversation("mine", owner_id="local")
    store.create_conversation("theirs", owner_id="someone_else")
    await store.update_title_unique("theirs", TITLE)
    assert await store.update_title_unique("mine", TITLE) == TITLE


async def test_reclaiming_its_own_title_does_not_stack_counters(store: SqliteEventStore) -> None:
    store.create_conversation("c1", owner_id="local")
    await store.update_title_unique("c1", TITLE)
    assert await store.update_title_unique("c1", TITLE) == TITLE


async def test_like_wildcards_in_a_title_are_literal(store: SqliteEventStore) -> None:
    """A '%'/'_' in the title must not make every row look like a collision."""
    weird = "100% Off_Grid Planner"
    store.create_conversation("c1", owner_id="local")
    store.create_conversation("c2", owner_id="local")
    store.create_conversation("c3", owner_id="local")
    await store.update_title_unique("c1", "1000 Off-Grid Planner (2)")  # LIKE-only match
    assert await store.update_title_unique("c2", weird) == weird
    assert await store.update_title_unique("c3", weird) == f"{weird} (2)"


async def test_unknown_conversation_is_a_no_op(store: SqliteEventStore) -> None:
    assert await store.update_title_unique("nope", TITLE) == TITLE
    assert await store.get_title("nope") is None

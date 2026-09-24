"""Read-only history creation and event persistence have one commit boundary."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from disco.core import EventSource, LLMMessage, MessageEvent, SqliteEventStore


@pytest.mark.parametrize("error", [ValueError, asyncio.CancelledError])
async def test_failed_import_rolls_back_identity_events_and_publishes_nothing(monkeypatch, error):
    store = SqliteEventStore(":memory:")
    persist = store._store_one
    published = Mock()
    calls = 0

    def fail_after_first(cid, event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error("interrupted import")
        return persist(cid, event)

    monkeypatch.setattr(store, "_store_one", fail_after_first)
    monkeypatch.setattr(store, "_publish", published)
    events = [
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))
        for text in ("one", "two")
    ]
    with pytest.raises(error):
        await store.import_read_only_conversation(
            "conv_atomic",
            events,
            owner_id="local",
            title="import",
            surface="agent",
        )
    assert not await store.conversation_exists("conv_atomic")
    assert await store.get_events("conv_atomic") == []
    published.assert_not_called()


async def test_cancelled_lock_wait_never_creates_an_import_row():
    store = SqliteEventStore(":memory:")
    await store._write_lock.acquire()
    task = asyncio.create_task(
        store.import_read_only_conversation(
            "conv_waiting",
            [],
            owner_id="local",
            title="import",
            surface="agent",
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store._write_lock.release()
    assert not await store.conversation_exists("conv_waiting")


async def test_duplicate_event_ids_reject_instead_of_silently_losing_history():
    store = SqliteEventStore(":memory:")
    event = MessageEvent(
        source=EventSource.USER, message=LLMMessage(role="user", content="original")
    )
    with pytest.raises(ValueError, match="unique event IDs"):
        await store.import_read_only_conversation(
            "conv_duplicate",
            [event, event],
            owner_id="local",
            title="import",
            surface="agent",
        )
    assert not await store.conversation_exists("conv_duplicate")

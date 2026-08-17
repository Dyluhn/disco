"""S-W5 durable payload and slow-subscriber memory bounds."""

from __future__ import annotations

import asyncio

import pytest
from disco.core import EventSource, LLMMessage, MessageEvent, SqliteEventStore
from disco.core.store.sqlite import (
    EPHEMERAL_SUBSCRIBER_QUEUE_MAX,
    EVENT_SUBSCRIBER_QUEUE_MAX,
    EventPayloadTooLarge,
    SubscriberOverflow,
)


def _message(content: str) -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT,
        message=LLMMessage(role="assistant", content=content),
    )


async def test_oversized_event_is_rejected_atomically_and_does_not_burn_sequence():
    store = SqliteEventStore(":memory:")
    with pytest.raises(EventPayloadTooLarge):
        await store.append("c", _message("x" * (2 * 1024 * 1024)))
    assert await store.get_events("c") == []

    stored = await store.append("c", _message("small"))
    assert stored.seq == 1
    store.close()


async def test_durable_slow_subscriber_overflows_bounded_queue_and_replays():
    store = SqliteEventStore(":memory:")
    stream = await store.subscribe("c", after_seq=0)
    first_waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await store.append("c", _message("first"))
    assert (await first_waiter).seq == 1

    # The generator is paused at its first yield, so it cannot drain its live
    # queue. One more than the cap disconnects it with a replay marker.
    for index in range(EVENT_SUBSCRIBER_QUEUE_MAX + 1):
        await store.append("c", _message(f"queued-{index}"))
    with pytest.raises(SubscriberOverflow):
        await anext(stream)

    replay = await store.subscribe("c", after_seq=0)
    seen = [await anext(replay) for _ in range(EVENT_SUBSCRIBER_QUEUE_MAX + 2)]
    assert [event.seq for event in seen] == list(range(1, EVENT_SUBSCRIBER_QUEUE_MAX + 3))
    await replay.aclose()
    store.close()


async def test_ephemeral_queue_drops_oldest_and_retains_only_bounded_tail():
    store = SqliteEventStore(":memory:")
    stream = await store.subscribe_ephemeral("c")
    first_waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    store.publish_ephemeral("c", {"index": -1})
    assert await first_waiter == {"index": -1}

    count = EPHEMERAL_SUBSCRIBER_QUEUE_MAX + 7
    for index in range(count):
        store.publish_ephemeral("c", {"index": index})
    first_retained = await anext(stream)
    assert first_retained == {"index": count - EPHEMERAL_SUBSCRIBER_QUEUE_MAX}
    await stream.aclose()
    store.close()

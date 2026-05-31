"""Serialization contract — event-state-contract.md §8.5.

The serialization boundary is hard: events are persisted, streamed, and read by
future code. These tests pin round-trip fidelity, discriminated-union decoding,
and forward-compatibility.
"""

from __future__ import annotations

import pytest
from conftest import (
    action,
    agent_error,
    fatal,
    observation,
    status,
    tombstone,
    user_msg,
)
from perpleximanus.core import (
    ConversationStatus,
    EventAdapter,
    StatusEvent,
    event_from_json_dict,
    event_to_json_dict,
    migrate_event,
)
from pydantic import ValidationError

# One instance of every concrete event type.
ALL_EVENT_SAMPLES = [
    user_msg("a question"),
    action("reason", "shell", {"cmd": "ls"}),
    observation("evt_a", "files listed"),
    agent_error("tool exploded", action_id="evt_a"),
    tombstone(1, 4, "earlier work summarized"),
    status(ConversationStatus.RUNNING),
    fatal("MaxIterationsReached", "500 hit"),
]


@pytest.mark.parametrize("event", ALL_EVENT_SAMPLES, ids=lambda e: type(e).__name__)
def test_round_trip(event):
    """Every event survives dump -> migrate -> validate, equal to the original."""
    raw = event_to_json_dict(event)
    restored = event_from_json_dict(migrate_event(raw))
    assert restored == event
    assert type(restored) is type(event)


def test_round_trip_is_json_safe():
    """model_dump(mode='json') yields ISO datetimes and enum *values* (§4)."""
    raw = event_to_json_dict(status(ConversationStatus.PAUSED))
    assert raw["status"] == "PAUSED"  # enum by value, not name/index
    assert raw["source"] == "system"
    assert isinstance(raw["timestamp"], str)  # ISO string, not a datetime


def test_discriminator_decodes_heterogeneous_list():
    """A mixed JSON list deserializes to the correct concrete types via `kind`."""
    raw_list = [event_to_json_dict(e) for e in ALL_EVENT_SAMPLES]
    decoded = [event_from_json_dict(migrate_event(r)) for r in raw_list]
    assert [type(d).__name__ for d in decoded] == [type(e).__name__ for e in ALL_EVENT_SAMPLES]


def test_forward_compat_missing_optional_field_with_default():
    """An event dict missing a field that has a default still validates
    (backward-compatible field addition, §4 rule 2)."""
    raw = event_to_json_dict(status(ConversationStatus.IDLE))
    del raw["meta"]  # optional, has a default_factory
    del raw["detail"]  # optional, default None
    restored = event_from_json_dict(migrate_event(raw))
    assert isinstance(restored, StatusEvent)
    assert restored.meta == {}
    assert restored.detail is None


def test_unknown_future_kind_fails_loudly():
    """An unknown future `kind` raises, never silently mis-decodes (§8.5)."""
    raw = event_to_json_dict(user_msg())
    raw["kind"] = "telepathy_from_the_future"
    with pytest.raises(ValidationError):
        EventAdapter.validate_python(raw)


def test_migrate_event_is_pure_and_idempotent():
    """migrate_event must not mutate its input and must be a fixed point at v1."""
    raw = event_to_json_dict(user_msg())
    snapshot = dict(raw)
    once = migrate_event(raw)
    twice = migrate_event(once)
    assert raw == snapshot  # input untouched
    assert once == twice  # idempotent

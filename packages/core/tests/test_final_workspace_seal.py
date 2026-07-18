from __future__ import annotations

import pytest
from disco.core import (
    FinalWorkspaceSeal,
    ResourceKey,
    SqliteEventStore,
    WorkspaceVersionEvent,
    event_from_json_dict,
    event_to_json_dict,
)
from event_fakes import user_msg
from pydantic import ValidationError

_DIGEST = "a" * 64


def _seal(**updates: object) -> FinalWorkspaceSeal:
    values: dict[str, object] = {
        "scope": ResourceKey(namespace="workspace.tree", identifier="conversation-1"),
        "terminal_seq": 41,
        "latest_effect_seq": 40,
        "version_seq": 7,
        "tree_digest": _DIGEST,
        "file_count": 3,
        "total_bytes": 128,
    }
    values.update(updates)
    return FinalWorkspaceSeal.model_validate(values)


def test_old_workspace_version_event_without_seal_remains_valid() -> None:
    event = WorkspaceVersionEvent(version_seq=7, tree_digest=_DIGEST, trigger="finish")

    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, WorkspaceVersionEvent)
    assert restored.final_seal is None


def test_final_seal_round_trips_and_binds_the_version_event() -> None:
    event = WorkspaceVersionEvent(
        version_seq=7,
        tree_digest=_DIGEST,
        trigger="finish",
        final_seal=_seal(),
    ).model_copy(update={"seq": 42})

    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, WorkspaceVersionEvent)
    assert restored.final_seal == _seal()


@pytest.mark.parametrize(
    ("event_updates", "message"),
    [
        ({"version_seq": 8}, "version sequence"),
        ({"tree_digest": "b" * 64}, "tree digest"),
    ],
)
def test_version_event_rejects_a_mismatched_seal(
    event_updates: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "version_seq": 7,
        "tree_digest": _DIGEST,
        "trigger": "finish",
        "final_seal": _seal(),
    }
    values.update(event_updates)

    with pytest.raises(ValidationError, match=message):
        WorkspaceVersionEvent.model_validate(values)


def test_final_seal_rejects_an_effect_after_terminal() -> None:
    with pytest.raises(ValidationError, match="must precede terminal"):
        _seal(latest_effect_seq=42)


def test_final_seal_rejects_an_effect_at_the_terminal_sequence() -> None:
    with pytest.raises(ValidationError, match="must precede terminal"):
        _seal(latest_effect_seq=41)


def test_version_event_sequence_must_follow_the_terminal_fence() -> None:
    event = WorkspaceVersionEvent(
        version_seq=7,
        tree_digest=_DIGEST,
        trigger="finish",
        final_seal=_seal(),
    )

    with pytest.raises(ValidationError, match="must precede version event"):
        WorkspaceVersionEvent.model_validate(event.model_dump(mode="python") | {"seq": 41})


async def test_store_revalidates_terminal_fence_after_assigning_sequence() -> None:
    store = SqliteEventStore(":memory:")
    try:
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(latest_effect_seq=1),
        )

        with pytest.raises(ValidationError, match="must precede version event"):
            await store.append("conversation-1", event)

        assert await store.get_events("conversation-1") == []
    finally:
        store.close()


async def test_store_accepts_a_correctly_ordered_conversation_bound_seal() -> None:
    store = SqliteEventStore(":memory:")
    try:
        await store.append("conversation-1", user_msg("work"))
        await store.append("conversation-1", user_msg("terminal fence"))
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(terminal_seq=2, latest_effect_seq=1),
        )

        stored = await store.append("conversation-1", event)

        assert stored.seq == 3
        assert isinstance(stored, WorkspaceVersionEvent)
        assert stored.final_seal == event.final_seal
    finally:
        store.close()


async def test_store_rejects_a_seal_for_another_conversation() -> None:
    store = SqliteEventStore(":memory:")
    try:
        event = WorkspaceVersionEvent(
            version_seq=7,
            tree_digest=_DIGEST,
            trigger="finish",
            final_seal=_seal(scope=ResourceKey(namespace="workspace.tree", identifier="other")),
        )

        with pytest.raises(ValueError, match="scope must match"):
            await store.append("conversation-1", event)
    finally:
        store.close()

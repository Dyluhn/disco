"""Canonical app handoff authority across durable workspace views."""

from __future__ import annotations

from disco.core import DeliverableEvent, SqliteEventStore, WorkspaceMutationEvent
from disco.core.loop.finish.common import _latest_app_deliverable_event


async def _admit_view(
    store: SqliteEventStore,
    conversation_id: str,
    *,
    view_id: str,
) -> None:
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
    )
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            run_protocol_version=1,
            agent_view_id=view_id,
        ),
    )


async def test_prior_revision_app_handoff_cannot_authorize_current_view() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_view"
    await _admit_view(store, conversation_id, view_id="view-old")
    await store.append(
        conversation_id,
        DeliverableEvent(
            title="Old application",
            path="old/index.html",
            artifact_kind="app",
            agent_view_id="view-old",
        ),
    )
    await _admit_view(store, conversation_id, view_id="view-current")

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) is None


async def test_current_app_handoff_survives_later_file_attachment() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_file"
    await _admit_view(store, conversation_id, view_id="view-current")
    app = await store.append(
        conversation_id,
        DeliverableEvent(
            title="Current application",
            path="dist/index.html",
            artifact_kind="app",
            agent_view_id="view-current",
        ),
    )
    await store.append(
        conversation_id,
        DeliverableEvent(
            title="Source attachment",
            path="notes.txt",
            artifact_kind="files",
            agent_view_id="view-current",
        ),
    )

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) == app

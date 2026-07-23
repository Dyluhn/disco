"""Canonical app handoff authority across durable workspace views."""

from __future__ import annotations

from disco.core import (
    DeliverableEvent,
    SqliteEventStore,
    WorkspaceMutationEvent,
    event_matches_current_workspace_intent,
)
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
    old_app = await store.append(
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
    assert not event_matches_current_workspace_intent(events, old_app)


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
    assert event_matches_current_workspace_intent(events, app)


async def test_current_app_handoff_survives_later_views_in_same_run_intent() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_later_view"
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
            agent_view_id="view-handoff",
        ),
    )
    app = await store.append(
        conversation_id,
        DeliverableEvent(
            title="Current application",
            path="index.html",
            artifact_kind="app",
            agent_view_id="view-handoff",
        ),
    )
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            run_protocol_version=1,
            agent_view_id="view-plan-verifier",
        ),
    )
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            run_protocol_version=1,
            agent_view_id="view-finish",
        ),
    )

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) == app
    assert event_matches_current_workspace_intent(events, app)


async def test_late_old_view_handoff_cannot_cross_new_run_intent() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_late_old_view"
    await _admit_view(store, conversation_id, view_id="view-old")
    current_intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
    )
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=current_intent.id,
            run_protocol_version=1,
            agent_view_id="view-current",
        ),
    )
    late_app = await store.append(
        conversation_id,
        DeliverableEvent(
            title="Late stale application",
            path="stale/index.html",
            artifact_kind="app",
            agent_view_id="view-old",
        ),
    )

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) is None
    assert not event_matches_current_workspace_intent(events, late_app)


async def test_late_old_view_handoff_cannot_land_after_newer_same_intent_view() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_late_same_intent"
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    for view_id in ("view-old", "view-current"):
        await store.append(
            conversation_id,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                run_protocol_version=1,
                agent_view_id=view_id,
            ),
        )
    late_app = await store.append(
        conversation_id,
        DeliverableEvent(
            title="Late stale application",
            path="stale/index.html",
            artifact_kind="app",
            agent_view_id="view-old",
        ),
    )

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) is None
    assert not event_matches_current_workspace_intent(events, late_app)


async def test_legacy_handoff_without_typed_view_admission_remains_readable() -> None:
    store = SqliteEventStore(":memory:")
    conversation_id = "conv_handoff_legacy"
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
    )
    await store.append(
        conversation_id,
        WorkspaceMutationEvent(operation="agent.run-admitted"),
    )
    app = await store.append(
        conversation_id,
        DeliverableEvent(
            title="Imported legacy application",
            path="index.html",
            artifact_kind="app",
        ),
    )

    events = await store.get_events(conversation_id)

    assert _latest_app_deliverable_event(events) == app
    assert event_matches_current_workspace_intent(events, app)

"""Strict resolver for the immutable workspace currently committed by FINISHED."""

from __future__ import annotations

from dataclasses import dataclass

from disco.core import (
    Event,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
    pending_workspace_run_intent,
)
from disco.tools.projects import ProjectStore, VersionRecord


class WorkspaceCommitUnavailable(RuntimeError):
    """The current event-log head has no trustworthy immutable workspace."""


class WorkspaceRunSuperseded(WorkspaceCommitUnavailable):
    """A stale worker tried to finalize after a newer durable view won."""


@dataclass(frozen=True)
class CommittedWorkspaceView:
    event: WorkspaceVersionEvent
    record: VersionRecord
    event_head_seq: int


def _canonical_event_head(events: list[Event]) -> int:
    if not events or any(type(event.seq) is not int or event.seq < 1 for event in events):
        raise WorkspaceCommitUnavailable("committed workspace requires a canonical event head")
    return max(event.seq for event in events if event.seq is not None)


def _final_seal_for_fence(
    events: list[Event],
    terminal_seq: int,
) -> WorkspaceVersionEvent:
    candidates = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent)
        and event.final_seal is not None
        and event.final_seal.terminal_seq == terminal_seq
    ]
    if not candidates:
        raise WorkspaceCommitUnavailable("FINISHED workspace has no final seal")
    return max(candidates, key=lambda candidate: candidate.seq or -1)


def _validate_final_seal(
    event: WorkspaceVersionEvent,
    events: list[Event],
    *,
    conversation_id: str,
    terminal_seq: int,
    latest_effect_seq: int | None,
) -> None:
    seal = event.final_seal
    assert seal is not None
    if event.seq is None or event.seq <= terminal_seq:
        raise WorkspaceCommitUnavailable("final seal event is not canonically sequenced")
    if event.trigger != "finish":
        raise WorkspaceCommitUnavailable("final seal event is not finish-triggered")
    if (
        seal.scope.namespace != "workspace.tree"
        or seal.scope.identifier != conversation_id
        or seal.latest_effect_seq != latest_effect_seq
        or seal.version_seq != event.version_seq
        or seal.tree_digest != event.tree_digest
    ):
        raise WorkspaceCommitUnavailable("final seal event disagrees with its event-log fence")
    if any(
        isinstance(version, WorkspaceVersionEvent)
        and version.seq is not None
        and version.seq > event.seq
        for version in events
    ):
        raise WorkspaceCommitUnavailable("a later uncommitted workspace version exists")


def _verify_immutable_record(
    project_store: ProjectStore,
    conversation_id: str,
    event: WorkspaceVersionEvent,
) -> VersionRecord:
    try:
        record = project_store.verify_version(conversation_id, event.version_seq)
    except Exception as exc:
        raise WorkspaceCommitUnavailable(f"immutable workspace proof failed: {exc}") from exc
    seal = event.final_seal
    assert seal is not None
    if (
        seal.file_count != record.file_count
        or seal.total_bytes != record.total_bytes
        or seal.tree_digest != record.tree_digest
    ):
        raise WorkspaceCommitUnavailable("final seal facts disagree with immutable workspace")
    return record


def resolve_committed_workspace(
    events: list[Event],
    project_store: ProjectStore,
    conversation_id: str,
) -> CommittedWorkspaceView:
    """Resolve and freshly prove the current FINISHED workspace commit.

    This is the sole authority for committed consumers. It never falls back to
    a live sandbox or mutable ProjectStore mirror. Later effects/status changes
    make the seal historical; later unsealed version markers make current state
    ambiguous and therefore unavailable.
    """

    if pending_workspace_run_intent(events) is not None:
        raise WorkspaceCommitUnavailable("workspace has unprocessed agent run intent")
    event_head_seq = _canonical_event_head(events)
    try:
        terminal_seq, latest_effect_seq = derive_final_workspace_fence(events)
    except ValueError as exc:
        raise WorkspaceCommitUnavailable(str(exc)) from exc
    event = _final_seal_for_fence(events, terminal_seq)
    _validate_final_seal(
        event,
        events,
        conversation_id=conversation_id,
        terminal_seq=terminal_seq,
        latest_effect_seq=latest_effect_seq,
    )
    record = _verify_immutable_record(project_store, conversation_id, event)
    return CommittedWorkspaceView(event=event, record=record, event_head_seq=event_head_seq)


__all__ = [
    "CommittedWorkspaceView",
    "WorkspaceCommitUnavailable",
    "WorkspaceRunSuperseded",
    "pending_workspace_run_intent",
    "resolve_committed_workspace",
]

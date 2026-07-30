"""Journal and immutable-version publication mechanics for workspace persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from disco.core import (
    ConversationStatus,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
    workspace_terminal_matches_current_run,
)
from disco.tools.projects import ProjectStore, StorageStatus, VersionRecord, WorkspaceTreeFacts

from .workspace_commit import WorkspaceRunSuperseded, pending_workspace_run_intent
from .workspace_finalization import (
    FINALIZATION_JOURNAL,
    checkpoint_event,
    clear_finalization_journal,
    journal_facts,
    seal_event,
    write_finalization_journal,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .project_runtime_service import ProjectRuntimeService
    from .workspace_fence import WorkspaceFenceService

_LOG = logging.getLogger(__name__)


def cut_recovery_version(
    store: ProjectStore,
    conversation_id: str,
    trigger: str,
    label: str,
) -> VersionRecord | None:
    version = (
        store.cut_version(conversation_id, label=label, trigger=trigger)
        if label
        else store.cut_version(conversation_id, trigger=trigger)
    )
    if version is not None:
        return version
    versions = store.list_versions(conversation_id)
    return versions[0] if versions else None


def facts_match_record(facts: WorkspaceTreeFacts, record: VersionRecord) -> bool:
    return (
        facts.file_count == record.file_count
        and facts.total_bytes == record.total_bytes
        and facts.tree_digest == record.tree_digest
    )


async def require_seal_publication_stable(
    event_store: SqliteEventStore,
    conversation_id: str,
    project_store: ProjectStore,
    seal_fence: tuple[int, int | None],
    version: VersionRecord,
    *,
    host_mirror: bool,
) -> None:
    events = await event_store.get_events(conversation_id)
    if pending_workspace_run_intent(events) is not None:
        raise RuntimeError("workspace has unprocessed agent run intent")
    if derive_final_workspace_fence(events) != seal_fence:
        raise RuntimeError("workspace event head changed during finalization")
    if host_mirror and not facts_match_record(
        project_store.inspect_workspace(conversation_id),
        version,
    ):
        raise RuntimeError("host mirror changed during finalization")


async def publish_strict_version(
    event_store: SqliteEventStore,
    conversation_id: str,
    store: ProjectStore,
    seal_fence: tuple[int, int | None],
    facts: WorkspaceTreeFacts,
    journal: dict[str, Any] | None,
    *,
    host_mirror: bool,
) -> WorkspaceVersionEvent:
    version = store.cut_verified_version(conversation_id, trigger="finish", pin=True)
    if version is None:
        raise RuntimeError("verified version cut returned no version")
    if host_mirror and not facts_match_record(facts, version):
        raise RuntimeError("host mirror changed during finalization")
    await require_seal_publication_stable(
        event_store,
        conversation_id,
        store,
        seal_fence,
        version,
        host_mirror=host_mirror,
    )
    if journal is not None:
        journal.update(
            {
                "phase": "version",
                "version_seq": version.seq,
                "file_count": version.file_count,
                "total_bytes": version.total_bytes,
                "tree_digest": version.tree_digest,
            }
        )
        write_finalization_journal(store, conversation_id, journal)
    checkpoint = await event_store.append(
        conversation_id,
        checkpoint_event(seal_fence, version),
    )
    if not isinstance(checkpoint, WorkspaceVersionEvent):
        raise RuntimeError("event store returned wrong finalization checkpoint type")
    await require_seal_publication_stable(
        event_store,
        conversation_id,
        store,
        seal_fence,
        version,
        host_mirror=host_mirror,
    )
    if journal is not None:
        journal.update({"phase": "checkpoint", "checkpoint_event_id": checkpoint.id})
        write_finalization_journal(store, conversation_id, journal)
    stored = await event_store.append(
        conversation_id,
        seal_event(conversation_id, seal_fence, version),
    )
    if not isinstance(stored, WorkspaceVersionEvent):
        raise RuntimeError("event store returned wrong final seal event type")
    try:
        await require_seal_publication_stable(
            event_store,
            conversation_id,
            store,
            seal_fence,
            version,
            host_mirror=host_mirror,
        )
    except Exception:
        await event_store.append(
            conversation_id,
            WorkspaceMutationEvent(operation="host.finalization-invalidated"),
        )
        raise
    if journal is not None:
        journal.update({"phase": "sealed", "seal_event_id": stored.id})
        write_finalization_journal(store, conversation_id, journal)
    return stored


async def _require_inactive_finished_head(
    event_store: SqliteEventStore,
    workspace: WorkspaceFenceService,
    conversation_id: str,
) -> None:
    if workspace.has_run_claim(conversation_id):
        raise RuntimeError("cannot seal a host revision while an agent run is active")
    current = await event_store.get_state(conversation_id)
    if current.execution_status is not ConversationStatus.FINISHED:
        raise RuntimeError("host revision can be sealed only from an inactive FINISHED head")


def _persisted_terminal_retry(
    events: list[Any],
    terminal_event: StatusEvent,
) -> StatusEvent | None:
    same_id = [event for event in events if event.id == terminal_event.id]
    if not same_id:
        return None
    if len(same_id) != 1:
        raise RuntimeError("terminal event id collides with another persisted event")
    existing = same_id[0]
    if not isinstance(existing, StatusEvent):
        raise RuntimeError("terminal event id collides with another persisted event")
    if existing.status is not ConversationStatus.FINISHED:
        raise RuntimeError("terminal event id was reused with a different payload")
    if existing.model_dump(exclude={"seq"}) != terminal_event.model_dump(exclude={"seq"}):
        raise RuntimeError("terminal event id was reused with a different payload")
    try:
        fence = derive_final_workspace_fence(events)
    except ValueError as exc:
        raise WorkspaceRunSuperseded(
            "persisted terminal is no longer the current workspace fence"
        ) from exc
    if existing.seq != fence[0]:
        raise WorkspaceRunSuperseded("persisted terminal is no longer the current workspace fence")
    return existing


def _validate_new_terminal(
    events: list[Any],
    terminal_event: StatusEvent,
    *,
    require_inactive_finished_head: bool,
) -> None:
    terminal_matches = workspace_terminal_matches_current_run(events, terminal_event)
    if require_inactive_finished_head:
        if terminal_event.host_mutation_id is None:
            raise RuntimeError("host revision terminal lacks matching mutation authority")
        if not terminal_matches:
            raise RuntimeError("host revision terminal lacks matching mutation authority")
    elif not terminal_matches:
        raise WorkspaceRunSuperseded("cannot seal output from a stale agent view generation")
    if pending_workspace_run_intent(events) is None:
        return
    if require_inactive_finished_head:
        raise RuntimeError("cannot seal while an agent run intent is unprocessed")


async def terminal_preflight(
    event_store: SqliteEventStore,
    workspace: WorkspaceFenceService,
    conversation_id: str,
    terminal_event: StatusEvent,
    *,
    require_inactive_finished_head: bool,
) -> StatusEvent | None:
    if require_inactive_finished_head:
        await _require_inactive_finished_head(event_store, workspace, conversation_id)
    events = await event_store.get_events(conversation_id)
    existing = _persisted_terminal_retry(events, terminal_event)
    if existing is not None:
        return existing
    _validate_new_terminal(
        events,
        terminal_event,
        require_inactive_finished_head=require_inactive_finished_head,
    )
    return None


async def drain_finalizer(
    finalizer: asyncio.Task[tuple[StatusEvent, WorkspaceVersionEvent | None]],
    conversation_id: str,
) -> tuple[StatusEvent, WorkspaceVersionEvent | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not finalizer.done():
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError as exc:
            if finalizer.cancelled():
                raise
            cancellation = cancellation or exc
    try:
        stored, sealed = finalizer.result()
    except Exception:
        if cancellation is not None:
            _LOG.error(
                "terminal workspace pipeline failed while draining cancellation for %s",
                conversation_id,
                exc_info=True,
            )
            raise cancellation from None
        raise
    return stored, sealed, cancellation


def _journal_identity(
    path: Any,
    conversation_id: str,
) -> dict[str, Any]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("journal root is not an object")
    if (
        raw.get("schema_version") != 1
        or raw.get("conversation_id") != conversation_id
        or not isinstance(raw.get("terminal_event_id"), str)
    ):
        raise ValueError("journal identity is invalid")
    return raw


def _checkpoint_version(
    store: ProjectStore,
    conversation_id: str,
    journal: dict[str, Any],
    events: list[Any],
    fence: tuple[int, int | None],
) -> VersionRecord:
    if journal.get("terminal_seq") != fence[0]:
        raise ValueError("finalization journal fence is invalid")
    if journal.get("latest_effect_seq") != fence[1]:
        raise ValueError("finalization journal fence is invalid")
    facts = journal_facts(journal)
    version_seq = journal.get("version_seq")
    if type(version_seq) is not int:
        raise ValueError("journal version sequence is invalid")
    if version_seq < 1:
        raise ValueError("journal version sequence is invalid")
    version = store.verify_version(conversation_id, version_seq)
    if not facts_match_record(facts, version):
        raise ValueError("immutable version disagrees with journal")
    if not facts_match_record(store.inspect_workspace(conversation_id), version):
        raise ValueError("live workspace disagrees with recovered version")
    checkpoint = _matching_checkpoint(events, fence, version)
    if checkpoint is None:
        raise ValueError("finalization version lacks its SQLite checkpoint")
    checkpoint_id = journal.get("checkpoint_event_id")
    if not isinstance(checkpoint_id, str):
        raise ValueError("journal checkpoint identity is invalid")
    if checkpoint_id != checkpoint.id:
        raise ValueError("journal checkpoint identity is invalid")
    return version


def _matching_checkpoint(
    events: list[Any],
    fence: tuple[int, int | None],
    version: VersionRecord,
) -> WorkspaceVersionEvent | None:
    return next(
        (
            event
            for event in events
            if isinstance(event, WorkspaceVersionEvent)
            and event.final_seal is None
            and event.version_seq == version.seq
            and event.tree_digest == version.tree_digest
            and event.trigger == f"finalizing:{fence[0]}"
        ),
        None,
    )


def _journal_terminal(
    events: list[Any],
    terminal_event_id: str,
) -> StatusEvent | None:
    return next(
        (
            event
            for event in events
            if isinstance(event, StatusEvent)
            and event.id == terminal_event_id
            and event.status is ConversationStatus.FINISHED
        ),
        None,
    )


def _existing_final_seal(
    events: list[Any],
    terminal_seq: int,
) -> WorkspaceVersionEvent | None:
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event, WorkspaceVersionEvent)
            and event.final_seal is not None
            and event.final_seal.terminal_seq == terminal_seq
        ),
        None,
    )


def _validate_existing_final_seal(
    store: ProjectStore,
    conversation_id: str,
    event: WorkspaceVersionEvent,
    fence: tuple[int, int | None],
) -> None:
    record = store.verify_version(conversation_id, event.version_seq)
    seal = event.final_seal
    if seal is None:
        raise ValueError("persisted final workspace event lacks a seal")
    if seal.latest_effect_seq != fence[1]:
        raise ValueError("persisted seal disagrees with immutable version")
    if seal.file_count != record.file_count:
        raise ValueError("persisted seal disagrees with immutable version")
    if seal.total_bytes != record.total_bytes:
        raise ValueError("persisted seal disagrees with immutable version")
    if seal.tree_digest != record.tree_digest:
        raise ValueError("persisted seal disagrees with immutable version")


async def _recover_one(
    event_store: SqliteEventStore,
    store: ProjectStore,
    conversation_id: str,
    journal: dict[str, Any],
) -> bool:
    events = await event_store.get_events(conversation_id)
    if pending_workspace_run_intent(events) is not None:
        raise ValueError("journal head has an unprocessed agent run intent")
    terminal = _journal_terminal(events, journal["terminal_event_id"])
    if terminal is None:
        if journal.get("phase") == "prepared":
            clear_finalization_journal(store, conversation_id)
        return False
    fence = derive_final_workspace_fence(events)
    if terminal.seq != fence[0]:
        raise ValueError("journal terminal is no longer the canonical fence")
    existing = _existing_final_seal(events, fence[0])
    if existing is not None:
        _validate_existing_final_seal(store, conversation_id, existing, fence)
        clear_finalization_journal(store, conversation_id)
        return True
    phase = journal.get("phase")
    if phase in {"prepared", "terminal", "capturing", "snapshot", "version"}:
        return False
    if phase not in {"checkpoint", "sealed"}:
        raise ValueError(f"unknown finalization journal phase: {phase!r}")
    version = _checkpoint_version(store, conversation_id, journal, events, fence)
    stored = await event_store.append(
        conversation_id,
        seal_event(conversation_id, fence, version),
    )
    if not isinstance(stored, WorkspaceVersionEvent):
        raise RuntimeError("event store returned wrong recovered seal type")
    clear_finalization_journal(store, conversation_id)
    return True


async def recover_finalization_journals(
    event_store: SqliteEventStore,
    workspace: WorkspaceFenceService,
    projects: ProjectRuntimeService,
) -> int:
    store = projects.current_project_store()
    if store is None or store.status() is not StorageStatus.OK or store.root is None:
        return 0
    try:
        root = store.root.resolve()
        project_dirs = sorted(
            path
            for path in root.iterdir()
            if not path.is_symlink() and path.is_dir() and path.resolve().is_relative_to(root)
        )
    except OSError:
        _LOG.warning("finalization journal scan failed", exc_info=True)
        return 0
    recovered = 0
    for project_dir in project_dirs:
        path = project_dir / FINALIZATION_JOURNAL
        if path.is_symlink() or not path.is_file():
            continue
        conversation_id = project_dir.name
        lock = workspace.lock(conversation_id)
        async with lock, workspace.interprocess_mutation_fence(conversation_id):
            try:
                journal = _journal_identity(path, conversation_id)
                recovered += int(
                    await _recover_one(
                        event_store,
                        store,
                        conversation_id,
                        journal,
                    )
                )
            except Exception:
                _LOG.error(
                    "finalization journal recovery failed for %s; workspace remains unsealed",
                    conversation_id,
                    exc_info=True,
                )
    return recovered

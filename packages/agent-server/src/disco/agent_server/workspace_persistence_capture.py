"""Workspace snapshot capture mechanics without transaction or revision authority."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from disco.core import DEFAULT_OWNER_ID, WorkspaceVersionEvent
from disco.core.loop import SealabilityProbeResult
from disco.tools.projects import ProjectStore, StorageStatus, WorkspaceTreeFacts

from .workspace_finalization import write_finalization_journal
from .workspace_persistence_journal import cut_recovery_version

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .persistence_notifier import PersistenceNotifier
    from .project_runtime_service import ProjectRuntimeService
    from .run_registry import RunResourceRegistry

_LOG = logging.getLogger(__name__)

_ALLOWED_SKIP_SUFFIXES = (
    "dependency/cache path excluded",
    "runtime secret path excluded",
)
SEAL_INCOMPLETE_CONTENT_KIND = "seal_incomplete_content"
_CONTENT_SKIP_SUFFIXES = (
    "symlink excluded",
    "non-regular entry excluded",
    "hardlinked entry excluded",
    "-byte cap",
)


def strict_blocking_skips(skipped: list[str]) -> list[str]:
    return [item for item in skipped if not item.endswith(_ALLOWED_SKIP_SUFFIXES)]


def content_blocking_skips(skipped: list[str]) -> list[str]:
    return [
        item for item in strict_blocking_skips(skipped) if item.endswith(_CONTENT_SKIP_SUFFIXES)
    ]


class FinalSealIncompleteContent(RuntimeError):
    """A strict seal refused deterministic workspace content."""

    def __init__(self, blocking: list[str], content: list[str]) -> None:
        super().__init__("final workspace snapshot was incomplete: " + "; ".join(blocking[:8]))
        self.blocking = list(blocking)
        self.content_blocking = list(content)


def seal_refusal_meta(exc: BaseException) -> dict[str, Any] | None:
    if not isinstance(exc, FinalSealIncompleteContent):
        return None
    return {
        "persistence_failure": {
            "kind": SEAL_INCOMPLETE_CONTENT_KIND,
            "blocking": exc.content_blocking[:32],
        }
    }


async def probe_finish_sealability(
    run_resources: RunResourceRegistry,
    conversation_id: str,
    *,
    snapshot_fn: Callable[..., Any] | None,
) -> SealabilityProbeResult:
    executor = run_resources.executor(conversation_id)
    session = getattr(executor, "_sandbox", None) if executor is not None else None
    if session is None or snapshot_fn is None:
        return SealabilityProbeResult(sealable=True, detail="no live workspace to probe")
    temporary = Path(tempfile.mkdtemp(prefix=".disco-seal-probe-"))
    try:
        result = await snapshot_fn(session, temporary / "tree")
        blocking = content_blocking_skips(list(result.skipped))
        return SealabilityProbeResult(sealable=not blocking, blocking=tuple(blocking[:64]))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def skipped_capture(conversation_id: str, trigger: str) -> None:
    _LOG.warning(
        "workspace capture SKIPPED for %s (trigger=%s): no capture session could be "
        "resolved; nothing was persisted for this conversation",
        conversation_id,
        trigger,
    )
    return None


async def conversation_manifest_metadata(
    event_store: SqliteEventStore,
    conversation_id: str,
) -> tuple[str | None, str | None, str | None]:
    try:
        summaries = await event_store.list_conversation_summaries(
            owner_id=DEFAULT_OWNER_ID,
            limit=500,
            cursor=None,
        )
        row = next(
            (summary for summary in summaries if summary.conversation_id == conversation_id),
            None,
        )
        if row is not None:
            return row.title, row.created_at, row.owner_id
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass
    return None, None, None


async def resolve_capture_session(
    projects: ProjectRuntimeService,
    run_resources: RunResourceRegistry,
    notifier: PersistenceNotifier,
    conversation_id: str,
    *,
    seal_fence: tuple[int, int | None] | None = None,
    pinned_session: Any | None = None,
) -> tuple[Any, ProjectStore] | None:
    store = projects.current_project_store()
    if store is None:
        _LOG.warning("snapshot %s: no project store — workspace NOT persisted", conversation_id)
        if seal_fence is not None:
            raise RuntimeError("no project store available for final workspace seal")
        return None
    status = store.status()
    if status is not StorageStatus.OK:
        _LOG.warning(
            "snapshot %s: store status=%s — NOT saved",
            conversation_id,
            status.value,
        )
        await notifier.emit(
            conversation_id,
            f"project storage is {status.value}; this build was NOT saved.",
        )
        if seal_fence is not None:
            raise RuntimeError(f"project storage is {status.value}")
        return None
    executor = run_resources.executor(conversation_id)
    live_session = getattr(executor, "_sandbox", None) if executor is not None else None
    session = pinned_session
    if session is not None and live_session is not None and live_session is not session:
        _LOG.warning(
            "snapshot %s: pinned sandbox is a previous generation — sealing the live one",
            conversation_id,
        )
        session = live_session
    if session is None:
        session = live_session
    if session is None:
        _LOG.warning(
            "snapshot SKIPPED for %s: no sandbox (executor=%s) — workspace NOT persisted",
            conversation_id,
            type(executor).__name__ if executor is not None else None,
        )
        if seal_fence is not None:
            raise RuntimeError("no live sandbox available for final workspace seal")
        return None
    return session, store


async def _capture_facts(
    store: ProjectStore,
    conversation_id: str,
    *,
    host_mirror: bool,
    seal_fence: tuple[int, int | None] | None,
    session: Any,
    snapshot_fn: Callable[..., Any] | None,
) -> WorkspaceTreeFacts:
    inspect_workspace = getattr(store, "inspect_workspace", None)
    if host_mirror:
        if seal_fence is None:
            raise RuntimeError("host-mirror capture requires a final seal fence")
        if not callable(inspect_workspace):
            raise RuntimeError("project store cannot prove host-mirror facts")
        return cast(WorkspaceTreeFacts, inspect_workspace(conversation_id))
    if snapshot_fn is None:
        raise RuntimeError("sandbox capture function is unavailable")
    result = await snapshot_fn(session, store.path_for(conversation_id))
    if seal_fence is not None:
        blocking = strict_blocking_skips(list(result.skipped))
        if blocking:
            content = content_blocking_skips(list(result.skipped))
            if content:
                raise FinalSealIncompleteContent(blocking, content)
            raise RuntimeError(
                "final workspace snapshot was incomplete: " + "; ".join(blocking[:8])
            )
    if callable(inspect_workspace):
        return cast(WorkspaceTreeFacts, inspect_workspace(conversation_id))
    if seal_fence is not None:
        raise RuntimeError("project store cannot prove final workspace facts")
    return WorkspaceTreeFacts(
        file_count=result.file_count,
        total_bytes=result.total_bytes,
        tree_digest="0" * 64,
    )


async def _resolve_capture_store(
    projects: ProjectRuntimeService,
    resolver: Callable[..., Awaitable[tuple[Any, ProjectStore] | None]],
    conversation_id: str,
    *,
    host_mirror: bool,
    seal_fence: tuple[int, int | None] | None,
    pinned_session: Any | None,
) -> tuple[Any, ProjectStore] | None:
    if host_mirror:
        store = projects.current_project_store()
        if store is None or store.status() is not StorageStatus.OK:
            raise RuntimeError("project store unavailable for host-mirror finalization")
        return None, store
    return await resolver(
        conversation_id,
        seal_fence=seal_fence,
        pinned_session=pinned_session,
    )


async def _publish_capture(
    event_store: SqliteEventStore,
    store: ProjectStore,
    conversation_id: str,
    *,
    trigger: str,
    version_label: str,
    seal_fence: tuple[int, int | None] | None,
    facts: WorkspaceTreeFacts,
    journal: dict[str, Any] | None,
    host_mirror: bool,
    publish_strict_version: Callable[..., Awaitable[WorkspaceVersionEvent]],
) -> WorkspaceVersionEvent | None:
    try:
        if seal_fence is not None:
            return await publish_strict_version(
                conversation_id,
                store,
                seal_fence,
                facts,
                journal,
                host_mirror=host_mirror,
            )
        version = cut_recovery_version(store, conversation_id, trigger, version_label)
        if version is not None:
            stored = await event_store.append(
                conversation_id,
                WorkspaceVersionEvent(
                    version_seq=version.seq,
                    tree_digest=version.tree_digest,
                    trigger=trigger,
                ),
            )
            return stored if isinstance(stored, WorkspaceVersionEvent) else None
    except Exception:  # noqa: BLE001
        _LOG.warning(
            "version cut failed for %s after snapshot",
            conversation_id,
            exc_info=True,
        )
        if seal_fence is not None:
            raise
    return None


async def _persist_capture(
    event_store: SqliteEventStore,
    store: ProjectStore,
    conversation_id: str,
    facts: WorkspaceTreeFacts,
    *,
    title: str | None,
    created_at: str | None,
    owner_id: str | None,
    trigger: str,
    version_label: str,
    seal_fence: tuple[int, int | None] | None,
    journal: dict[str, Any] | None,
    host_mirror: bool,
    synthesize_deliverable: Callable[[str, Path], Awaitable[None]],
    publish_strict_version: Callable[..., Awaitable[WorkspaceVersionEvent]],
) -> WorkspaceVersionEvent | None:
    if journal is not None:
        journal.update(
            {
                "phase": "snapshot",
                "file_count": facts.file_count,
                "total_bytes": facts.total_bytes,
                "tree_digest": facts.tree_digest,
            }
        )
    store.write_manifest(
        conversation_id,
        title=title,
        owner_id=owner_id,
        created_at=created_at,
        file_count=facts.file_count,
        total_bytes=facts.total_bytes,
    )
    await synthesize_deliverable(conversation_id, store.path_for(conversation_id))
    if journal is not None:
        write_finalization_journal(store, conversation_id, journal)
    return await _publish_capture(
        event_store,
        store,
        conversation_id,
        trigger=trigger,
        version_label=version_label,
        seal_fence=seal_fence,
        facts=facts,
        journal=journal,
        host_mirror=host_mirror,
        publish_strict_version=publish_strict_version,
    )


async def capture_workspace(
    event_store: SqliteEventStore,
    projects: ProjectRuntimeService,
    notifier: PersistenceNotifier,
    conversation_id: str,
    *,
    trigger: str,
    version_label: str,
    seal_fence: tuple[int, int | None] | None,
    journal: dict[str, Any] | None,
    snapshot_fn: Callable[..., Any] | None,
    host_mirror: bool,
    pinned_session: Any | None,
    resolve_session: Callable[..., Awaitable[tuple[Any, ProjectStore] | None]],
    synthesize_deliverable: Callable[[str, Path], Awaitable[None]],
    publish_strict_version: Callable[..., Awaitable[WorkspaceVersionEvent]],
) -> WorkspaceVersionEvent | None:
    resolved = await _resolve_capture_store(
        projects,
        resolve_session,
        conversation_id,
        host_mirror=host_mirror,
        seal_fence=seal_fence,
        pinned_session=pinned_session,
    )
    if resolved is None:
        return skipped_capture(conversation_id, trigger)
    session, store = resolved
    title, created_at, owner_id = await conversation_manifest_metadata(
        event_store,
        conversation_id,
    )
    started = time.monotonic()
    source = "host-mirror" if host_mirror else "sandbox"
    _LOG.info(
        "workspace capture started for %s (trigger=%s source=%s)",
        conversation_id,
        trigger,
        source,
    )
    try:
        facts = await _capture_facts(
            store,
            conversation_id,
            host_mirror=host_mirror,
            seal_fence=seal_fence,
            session=session,
            snapshot_fn=snapshot_fn,
        )
        _LOG.info(
            "workspace capture completed for %s: %d files, %d bytes in %.3fs (source=%s)",
            conversation_id,
            facts.file_count,
            facts.total_bytes,
            time.monotonic() - started,
            source,
        )
        return await _persist_capture(
            event_store,
            store,
            conversation_id,
            facts,
            title=title,
            created_at=created_at,
            owner_id=owner_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            journal=journal,
            host_mirror=host_mirror,
            synthesize_deliverable=synthesize_deliverable,
            publish_strict_version=publish_strict_version,
        )
    except asyncio.CancelledError:
        _LOG.warning(
            "snapshot canceled for %s after %.3fs (trigger=%s)",
            conversation_id,
            time.monotonic() - started,
            trigger,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - strict capture re-raises below
        _LOG.warning(
            "snapshot failed for %s after %.3fs (trigger=%s): %s",
            conversation_id,
            time.monotonic() - started,
            trigger,
            exc,
        )
        await notifier.emit(
            conversation_id,
            f"snapshot failed: {exc}",
            meta=seal_refusal_meta(exc),
        )
        if seal_fence is not None:
            raise
        return None

"""Workspace persistence transaction owner over bounded capture/journal mechanics."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core import (
    ConversationStatus,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
)
from disco.core.context.artifact_projection import manifest_shadow_enabled
from disco.tools.projects import ProjectStore, StorageStatus, WorkspaceTreeFacts

from .workspace_deliverables import (
    find_snapshot_index,
    maybe_synthesize_app_deliverable,
    trusted_verified_app_entry,
)
from .workspace_finalization import (
    clear_finalization_journal,
    write_finalization_journal,
)
from .workspace_persistence_capture import (
    SEAL_INCOMPLETE_CONTENT_KIND,
    FinalSealIncompleteContent,
    capture_workspace,
    content_blocking_skips,
    conversation_manifest_metadata,
    probe_finish_sealability,
    resolve_capture_session,
    seal_refusal_meta,
    skipped_capture,
    strict_blocking_skips,
)
from .workspace_persistence_journal import (
    cut_recovery_version,
    drain_finalizer,
    facts_match_record,
    publish_strict_version,
    recover_finalization_journals,
    require_seal_publication_stable,
    terminal_preflight,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .artifact_manifest_shadow import ArtifactManifestShadow
    from .persistence_notifier import PersistenceNotifier
    from .project_runtime_service import ProjectRuntimeService
    from .run_registry import RunRegistry, RunResourceRegistry
    from .workspace_fence import WorkspaceFenceService

_LOG = logging.getLogger(__name__)

# Preserve the original private module seams while their implementations live
# in bounded, stateless helpers.
_cut_recovery_version = cut_recovery_version
_facts_match_record = facts_match_record
_require_seal_publication_stable = require_seal_publication_stable
_seal_refusal_meta = seal_refusal_meta
_skipped_capture = skipped_capture


async def _publish_terminal_pipeline(
    event_store: SqliteEventStore,
    run_resources: RunResourceRegistry,
    artifact_manifest_shadow: ArtifactManifestShadow,
    conversation_id: str,
    terminal_event: StatusEvent,
    store: ProjectStore,
    journal: dict[str, Any],
    *,
    host_mirror: bool,
    snapshot_fn: Callable[..., Any] | None,
    capture: Callable[..., Awaitable[WorkspaceVersionEvent | None]],
) -> tuple[StatusEvent, WorkspaceVersionEvent | None]:
    """Append terminal and capture its exact immutable workspace fence."""

    pinned_executor = run_resources.executor(conversation_id)
    pinned_session = (
        getattr(pinned_executor, "_sandbox", None) if pinned_executor is not None else None
    )
    events = await event_store.get_events(conversation_id)
    if not host_mirror and manifest_shadow_enabled():
        await event_store.append(
            conversation_id,
            WorkspaceMutationEvent(
                operation="agent.artifact-manifest-fold",
                paths=(
                    ".disco/context/artifact_manifest.json",
                    ".disco/context/resource_manifest.json",
                ),
                agent_view_id=terminal_event.agent_view_id,
            ),
        )
        events = await event_store.get_events(conversation_id)
        await artifact_manifest_shadow.fold(conversation_id, events=events)
    stored = await event_store.append(conversation_id, terminal_event)
    if not isinstance(stored, StatusEvent) or stored.seq is None:
        raise RuntimeError("event store returned an unsequenced terminal event")
    try:
        events = await event_store.get_events(conversation_id)
        fence = derive_final_workspace_fence(events)
        if fence[0] != stored.seq:
            raise RuntimeError("persisted FINISHED is not the canonical terminal fence")
        journal.update(
            {
                "phase": "terminal",
                "terminal_seq": fence[0],
                "latest_effect_seq": fence[1],
            }
        )
        write_finalization_journal(store, conversation_id, journal)
        journal["phase"] = "capturing"
        write_finalization_journal(store, conversation_id, journal)
        sealed = await capture(
            conversation_id,
            trigger="finish",
            seal_fence=fence,
            journal=journal,
            snapshot_fn=snapshot_fn,
            host_mirror=host_mirror,
            pinned_session=pinned_session,
        )
        return stored, sealed
    except Exception:
        _LOG.error(
            "final workspace seal failed for %s; FINISHED remains unsealed",
            conversation_id,
            exc_info=True,
        )
        return stored, None


class WorkspacePersistence:
    """Own the workspace lock and terminal persistence transaction."""

    def __init__(
        self,
        store: SqliteEventStore,
        run_resources: RunResourceRegistry,
        workspace: WorkspaceFenceService,
        projects: ProjectRuntimeService,
        artifact_manifest_shadow: ArtifactManifestShadow,
        persistence_notifier: PersistenceNotifier,
        run_registry: RunRegistry,
    ) -> None:
        self._store = store
        self._run_resources = run_resources
        self._workspace = workspace
        self._projects = projects
        self._artifact_manifest_shadow = artifact_manifest_shadow
        self._persistence_notifier = persistence_notifier
        self._run_registry = run_registry

    async def _publish_strict_version(
        self,
        conversation_id: str,
        store: ProjectStore,
        seal_fence: tuple[int, int | None],
        facts: WorkspaceTreeFacts,
        journal: dict[str, Any] | None,
        *,
        host_mirror: bool,
    ) -> WorkspaceVersionEvent:
        return await publish_strict_version(
            self._store,
            conversation_id,
            store,
            seal_fence,
            facts,
            journal,
            host_mirror=host_mirror,
        )

    async def _do_commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
        snapshot_fn: Callable[..., Any],
    ) -> StatusEvent:
        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        lock = self._workspace.lock(conversation_id)
        async with lock:
            async with self._workspace.interprocess_mutation_fence(conversation_id):
                return await self._commit_finished_workspace_locked(
                    conversation_id,
                    terminal_event,
                    require_inactive_finished_head=require_inactive_finished_head,
                    host_mirror=False,
                    snapshot_fn=snapshot_fn,
                )

    async def _do_commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        lock = self._workspace.lock(conversation_id)
        if not lock.locked():
            raise RuntimeError("host-mirror finalization requires the conversation lock")
        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        async with self._workspace.interprocess_mutation_fence(conversation_id):
            return await self._commit_finished_workspace_locked(
                conversation_id,
                terminal_event,
                require_inactive_finished_head=True,
                host_mirror=True,
                snapshot_fn=None,
            )

    async def _commit_finished_workspace_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool,
        host_mirror: bool,
        snapshot_fn: Callable[..., Any] | None,
    ) -> StatusEvent:
        existing = await terminal_preflight(
            self._store,
            self._workspace,
            conversation_id,
            terminal_event,
            require_inactive_finished_head=require_inactive_finished_head,
        )
        if existing is not None:
            return existing
        store = self._projects.current_project_store()
        if store is None or store.status() is not StorageStatus.OK:
            unavailable = await self._store.append(conversation_id, terminal_event)
            if not isinstance(unavailable, StatusEvent):
                raise RuntimeError("event store returned wrong terminal event type")
            return unavailable
        journal: dict[str, Any] = {
            "schema_version": 1,
            "conversation_id": conversation_id,
            "terminal_event_id": terminal_event.id,
            "phase": "prepared",
        }
        try:
            write_finalization_journal(store, conversation_id, journal)
        except Exception as exc:
            _LOG.error(
                "could not prepare finalization journal for %s; FINISHED will be unsealed",
                conversation_id,
                exc_info=True,
            )
            unjournaled = await self._store.append(conversation_id, terminal_event)
            if not isinstance(unjournaled, StatusEvent):
                raise RuntimeError("event store returned wrong terminal event type") from exc
            return unjournaled
        finalizer = asyncio.create_task(
            _publish_terminal_pipeline(
                self._store,
                self._run_resources,
                self._artifact_manifest_shadow,
                conversation_id,
                terminal_event,
                store,
                journal,
                host_mirror=host_mirror,
                snapshot_fn=snapshot_fn,
                capture=self._do_capture_workspace,
            )
        )
        stored, sealed, cancellation = await drain_finalizer(finalizer, conversation_id)
        if sealed is not None:
            clear_finalization_journal(store, conversation_id)
        if cancellation is not None:
            raise cancellation
        return stored

    def _has_active_work(self, conversation_id: str) -> bool:
        task = self._run_registry.task(conversation_id)
        return task is not None and not task.done()

    async def _do_recover_finalization_journals(self) -> int:
        return await recover_finalization_journals(
            self._store,
            self._workspace,
            self._projects,
        )

    async def _conversation_manifest_metadata(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None, str | None]:
        return await conversation_manifest_metadata(self._store, conversation_id)

    async def _do_capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict[str, Any] | None = None,
        snapshot_fn: Callable[..., Any] | None,
        host_mirror: bool = False,
        pinned_session: Any | None = None,
    ) -> WorkspaceVersionEvent | None:
        resolver = self._resolve_capture_session
        if not host_mirror:
            resolved = await resolver(
                conversation_id,
                seal_fence=seal_fence,
                pinned_session=pinned_session,
            )
            if resolved is None:
                return _skipped_capture(conversation_id, trigger)

            async def reuse_resolved_session(
                _conversation_id: str,
                **_kwargs: Any,
            ) -> tuple[Any, ProjectStore] | None:
                return resolved

            resolver = reuse_resolved_session
        return await capture_workspace(
            self._store,
            self._projects,
            self._persistence_notifier,
            conversation_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            journal=journal,
            snapshot_fn=snapshot_fn,
            host_mirror=host_mirror,
            pinned_session=pinned_session,
            resolve_session=resolver,
            synthesize_deliverable=self._do_maybe_synthesize_app_deliverable,
            publish_strict_version=self._publish_strict_version,
        )

    async def _resolve_capture_session(
        self,
        conversation_id: str,
        *,
        seal_fence: tuple[int, int | None] | None = None,
        pinned_session: Any | None = None,
    ) -> tuple[Any, ProjectStore] | None:
        return await resolve_capture_session(
            self._projects,
            self._run_resources,
            self._persistence_notifier,
            conversation_id,
            seal_fence=seal_fence,
            pinned_session=pinned_session,
        )

    async def _do_maybe_synthesize_app_deliverable(
        self,
        conversation_id: str,
        snapshot_dir: Path,
    ) -> None:
        await maybe_synthesize_app_deliverable(
            self._store,
            conversation_id,
            snapshot_dir,
        )

    @staticmethod
    def _trusted_verified_app_entry(
        events: list[Any],
        snapshot_dir: Path,
    ) -> str | None:
        return trusted_verified_app_entry(events, snapshot_dir)

    @staticmethod
    def _find_snapshot_index(snapshot_dir: Path) -> Path | None:
        return find_snapshot_index(snapshot_dir)


__all__ = [
    "FinalSealIncompleteContent",
    "SEAL_INCOMPLETE_CONTENT_KIND",
    "WorkspacePersistence",
    "content_blocking_skips",
    "probe_finish_sealability",
    "strict_blocking_skips",
]

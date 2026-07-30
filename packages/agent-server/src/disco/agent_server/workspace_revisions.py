"""Committed-view and verified-restore operations for the workspace coordinator."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceRestoredEvent,
)
from disco.tools.projects import (
    StorageError,
    StorageStatus,
    VersionRecord,
    snapshot_workspace,
)

from .lifecycle_command_service import LifecycleCommandService
from .workspace_commit import (
    CommittedWorkspaceView,
    WorkspaceCommitUnavailable,
    resolve_committed_workspace,
)

if TYPE_CHECKING:
    import asyncio

    from disco.core.store.sqlite import SqliteEventStore

    from .project_runtime_service import ProjectRuntimeService
    from .workspace_fence import WorkspaceFenceService
    from .workspace_ownership import WorkspaceOwnership


class WorkspaceRestoreConflict(ValueError):
    """The workspace cannot be restored while a conversation is running."""


class WorkspaceVersionNotFound(LookupError):
    """The requested workspace version does not exist or is unverifiable."""


class WorkspaceRestoreStorageError(RuntimeError):
    """Workspace restore failed because storage or sandbox I/O failed."""


_LOG = logging.getLogger(__name__)


async def _apply_restore_to_session(
    session: Any,
    entries: Any,
    stage: Path,
    store: Any,
    conversation_id: str,
) -> None:
    """Apply staged verified bytes to one session and re-mirror the live store."""

    clear = await session.exec_shell(
        "find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
        timeout_s=30,
    )
    if clear.exit_code != 0:
        detail = (clear.stderr or clear.stdout or "workspace clear failed").strip()
        raise WorkspaceRestoreStorageError(detail[:300])
    for entry in entries:
        data = (stage / entry.path).read_bytes()
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise WorkspaceRestoreStorageError(
                f"verified restore stage changed before consumption: {entry.path}"
            )
        await session.write_file(entry.path, data)

    live_workspace = store.path_for(conversation_id)
    result = await snapshot_workspace(session, live_workspace)
    project_record = store.get(conversation_id)
    store.write_manifest(
        conversation_id,
        title=project_record.title if project_record is not None else None,
        owner_id=project_record.owner_id if project_record is not None else None,
        created_at=project_record.created_at if project_record is not None else None,
        file_count=result.file_count,
        total_bytes=result.total_bytes,
    )


class _WorkspaceRevisions:
    """Revision methods consuming, but never duplicating, coordinator authority."""

    if TYPE_CHECKING:
        _store: SqliteEventStore
        _projects: ProjectRuntimeService
        _lifecycle_commands: LifecycleCommandService
        _ownership: WorkspaceOwnership
        _fences: WorkspaceFenceService

        def lock(self, conversation_id: str) -> asyncio.Lock: ...

        def interprocess_mutation_fence(
            self,
            conversation_id: str,
            *,
            wait: bool = True,
        ) -> AbstractAsyncContextManager[None]: ...

        def _require_process_fence_locked(self, conversation_id: str) -> None: ...

        def has_run_claim(self, conversation_id: str) -> bool: ...

        async def record_mutation_locked(
            self,
            conversation_id: str,
            operation: str,
            *,
            paths: tuple[str, ...] = (),
        ) -> WorkspaceMutationEvent: ...

    async def _host_mutation_authority(
        self,
        conversation_id: str,
        operation: str,
    ) -> str:
        events = await self._store.get_events(conversation_id)
        latest_finished = max(
            (
                event.seq
                for event in events
                if isinstance(event, StatusEvent)
                and event.status is ConversationStatus.FINISHED
                and type(event.seq) is int
            ),
            default=-1,
        )
        candidates = [
            event
            for event in events
            if isinstance(event, WorkspaceMutationEvent)
            and event.operation == operation
            and type(event.seq) is int
            and event.seq > latest_finished
        ]
        if not candidates:
            raise WorkspaceCommitUnavailable(
                f"host revision {operation!r} has no current mutation authority"
            )
        return max(candidates, key=lambda event: event.seq or -1).id

    async def finalize_sandbox_change(
        self,
        conversation_id: str,
        operation: str,
    ) -> VersionRecord:
        """Seal an inactive FINISHED host edit by recapturing its sandbox."""

        mutation_id = await self._host_mutation_authority(conversation_id, operation)
        await self._lifecycle_commands.commit_finished_workspace(
            conversation_id,
            LifecycleCommandService.build_status(
                ConversationStatus.FINISHED,
                detail=f"host_revision:{operation}",
                host_mutation_id=mutation_id,
            ),
            require_inactive_finished_head=True,
        )
        return await self._resolve_latest_commit(conversation_id, operation)

    async def finalize_host_mirror_change_locked(
        self,
        conversation_id: str,
        operation: str,
    ) -> VersionRecord:
        """Seal host-owned mirror bytes without snapshotting a sandbox."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("host-mirror finalization requires the conversation lock")
        mutation_id = await self._host_mutation_authority(conversation_id, operation)
        await self._lifecycle_commands._commit_finished_host_mirror_locked(
            conversation_id,
            LifecycleCommandService.build_status(
                ConversationStatus.FINISHED,
                detail=f"host_mirror_revision:{operation}",
                host_mutation_id=mutation_id,
            ),
        )
        return await self._resolve_latest_commit(conversation_id, operation)

    async def require_committed_host_mirror_locked(
        self,
        conversation_id: str,
    ) -> CommittedWorkspaceView:
        """Require the mutable host mirror to equal the current immutable seal."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("committed host-mirror proof requires the conversation lock")
        if self.has_run_claim(conversation_id):
            raise WorkspaceCommitUnavailable("an agent run is active for this workspace")
        state = await self._store.get_state(conversation_id)
        if state.execution_status is not ConversationStatus.FINISHED:
            raise WorkspaceCommitUnavailable("workspace is not at an inactive FINISHED head")
        events = await self._store.get_events(conversation_id)
        committed = resolve_committed_workspace(
            events,
            self._projects.current_project_store(),
            conversation_id,
        )
        facts = self._projects.current_project_store().inspect_workspace(conversation_id)
        record = committed.record
        if (
            facts.file_count != record.file_count
            or facts.total_bytes != record.total_bytes
            or facts.tree_digest != record.tree_digest
        ):
            raise WorkspaceCommitUnavailable(
                "host workspace mirror has drifted from its current immutable seal"
            )
        return committed

    async def _resolve_latest_commit(
        self,
        conversation_id: str,
        operation: str,
    ) -> VersionRecord:
        events = await self._store.get_events(conversation_id)
        try:
            return resolve_committed_workspace(
                events,
                self._projects.current_project_store(),
                conversation_id,
            ).record
        except WorkspaceCommitUnavailable:
            raise
        except Exception as exc:
            raise WorkspaceCommitUnavailable(
                f"host workspace revision {operation!r} could not be sealed: {exc}"
            ) from exc

    async def restore_version(self, conversation_id: str, seq: int) -> dict:
        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                prior_state = await self._store.get_state(conversation_id)
                result = await self.restore_version_locked(conversation_id, seq)
        if prior_state.execution_status is ConversationStatus.FINISHED:
            try:
                committed = await self.finalize_sandbox_change(
                    conversation_id,
                    "version.restore",
                )
                result["new_version"] = committed.seq
            except Exception as exc:  # noqa: BLE001
                raise WorkspaceRestoreStorageError(
                    f"restored workspace could not be sealed: {exc}"
                ) from exc
        return result

    async def restore_version_locked(self, conversation_id: str, seq: int) -> dict:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("locked workspace restore requires the conversation lock")
        self._require_process_fence_locked(conversation_id)
        state = await self._store.get_state(conversation_id)
        if state.execution_status is ConversationStatus.RUNNING:
            raise WorkspaceRestoreConflict("conversation is running")

        store = self._projects.current_project_store()
        if store.status() is not StorageStatus.OK:
            raise WorkspaceRestoreStorageError(f"project storage is {store.status().value}")
        try:
            store.verify_version(conversation_id, seq)
        except StorageError as exc:
            raise WorkspaceVersionNotFound(str(exc)) from exc
        try:
            record = store.set_version_pinned(conversation_id, seq, True)
        except StorageError as exc:
            raise WorkspaceRestoreStorageError(str(exc)) from exc

        session = await self._ownership.restore_session(conversation_id)
        try:
            with tempfile.TemporaryDirectory(prefix="disco-verified-restore-") as raw_stage:
                stage = Path(raw_stage)
                with store.open_verified_version(conversation_id, seq) as verified:
                    entries = verified.files
                    for entry in entries:
                        target = stage / entry.path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(verified.read_bytes(entry.path))

                await self.record_mutation_locked(
                    conversation_id,
                    "version.restore",
                    paths=(".",),
                )
                try:
                    await _apply_restore_to_session(session, entries, stage, store, conversation_id)
                except Exception:  # noqa: BLE001
                    _LOG.warning(
                        "workspace restore apply failed for %s; reacquiring a session once",
                        conversation_id,
                        exc_info=True,
                    )
                    fresh = await self._ownership.restore_session(conversation_id, exclude=session)
                    await _apply_restore_to_session(fresh, entries, stage, store, conversation_id)
        except WorkspaceRestoreStorageError as exc:
            _LOG.error("workspace restore failed for %s: %s", conversation_id, exc)
            raise
        except StorageError as exc:
            _LOG.error("workspace restore failed for %s: %s", conversation_id, exc)
            raise WorkspaceRestoreStorageError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            _LOG.error("workspace restore failed for %s: %s", conversation_id, exc, exc_info=True)
            raise WorkspaceRestoreStorageError(str(exc)) from exc

        await self._append_restore_events(conversation_id, seq, record.tree_digest, record.label)
        try:
            new_record = store.cut_version(conversation_id, trigger="restore")
        except StorageError as exc:
            raise WorkspaceRestoreStorageError(str(exc)) from exc
        return {
            "restored": seq,
            "new_version": new_record.seq if new_record is not None else None,
            "tree_digest": record.tree_digest,
        }

    async def _append_restore_events(
        self,
        conversation_id: str,
        seq: int,
        tree_digest: str,
        label: str,
    ) -> None:
        await self._store.append(
            conversation_id,
            WorkspaceRestoredEvent(
                version_seq=seq,
                tree_digest=tree_digest,
                label=label,
            ),
        )
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"The user rolled the workspace back to version {seq} "
                        f"(digest {tree_digest}). Files on disk now reflect "
                        "that version — any writes you made after it NO LONGER EXIST "
                        "on disk. Re-read files before editing; do not rewrite from memory.\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )

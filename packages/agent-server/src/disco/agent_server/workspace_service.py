"""Per-conversation workspace coordination owned outside the runtime facade.

The coordinator owns the permanent lock registry and the operations that must
share that serialization domain: host mutation fences, terminal commit hooks,
version restore, and conversation disposal.  It receives its actual
collaborators directly — no runtime back-ref, no ``rt: Any``, no multi-domain
locator.  Disposal and restore-session acquisition are delegated to
:class:`WorkspaceOwnership` (see ``workspace_ownership.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceRestoredEvent,
    latest_workspace_run_intent,
)
from disco.core.loop import SealabilityProbeResult
from disco.tools.projects import (
    StorageError,
    StorageStatus,
    VersionRecord,
    snapshot_workspace,
)

from .lifecycle_command_service import LifecycleCommandService
from .runtime_settings import _BUILD_LIKE_SURFACES
from .workspace_commit import (
    CommittedWorkspaceView,
    WorkspaceCommitUnavailable,
    WorkspaceRunSuperseded,
    resolve_committed_workspace,
)
from .workspace_fence import WorkspaceFenceService
from .workspace_ownership import WorkspaceOwnership

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .build_platform_runtime import BuildPlatformRuntime
    from .lifecycle import LifecycleManager
    from .project_runtime_service import ProjectRuntimeService
    from .run_controller import RunController
    from .run_supervisor import RunPersistenceSupervisor
    from .runtime_settings import RuntimeSettings


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
    """Apply staged verified bytes to one session and re-mirror the live store.

    Everything session-dependent lives here so a dying-session failure can be
    retried ONCE on a reacquired session (F-28, pilot seed 620108: a restore
    1.7 s after FINISHED raced the executor teardown and died mid-apply). The
    stage is immutable input; a re-run re-clears and re-writes, so a partial
    first attempt cannot corrupt the second.
    """

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


class WorkspaceCoordinator:
    """Coordinate workspace operations through explicit named owners."""

    def __init__(
        self,
        store: SqliteEventStore,
        settings: RuntimeSettings,
        projects: ProjectRuntimeService,
        lifecycle_commands: LifecycleCommandService,
        lifecycle: LifecycleManager,
        build_platform: BuildPlatformRuntime,
        ownership: WorkspaceOwnership,
        fence_service: WorkspaceFenceService,
    ) -> None:
        self._store = store
        self._settings = settings
        self._projects = projects
        self._lifecycle_commands = lifecycle_commands
        self._lifecycle = lifecycle
        self._build_platform = build_platform
        self._ownership = ownership
        self._fences = fence_service
        self._run_controller: RunController | None = None
        self._run_execution: RunPersistenceSupervisor | None = None

    def bind_run_collaborators(
        self,
        run_controller: RunController,
        run_execution: RunPersistenceSupervisor,
    ) -> None:
        """Bind the run collaborators after they are wired.

        Called once from the composition root after :class:`RunController` and
        :class:`RunPersistenceSupervisor` exist.  This is an explicit typed
        binding — not a runtime back-ref, not a dynamic lookup, not a bag.
        """
        self._run_controller = run_controller
        self._run_execution = run_execution

    def lock(self, conversation_id: str) -> asyncio.Lock:
        return self._fences.lock(conversation_id)

    @asynccontextmanager
    async def interprocess_mutation_fence(
        self,
        conversation_id: str,
        *,
        wait: bool = True,
    ) -> AsyncIterator[None]:
        """Extend the caller-owned local fence across Agent server processes."""

        async with self._fences.interprocess_mutation_fence(
            conversation_id,
            wait=wait,
        ):
            yield

    def _fence_owned_by_current_task(self, conversation_id: str) -> bool:
        return self._fences.fence_owned_by_current_task(conversation_id)

    def _require_process_fence_locked(self, conversation_id: str) -> None:
        self._fences.require_process_fence_locked(conversation_id)

    async def run_after_admission(
        self,
        conversation_id: str,
        loop: Any,
        *,
        expected_run_intent_id: str | None = None,
    ) -> Any:
        """Admit a Build run through the same fence as host mutations.

        The task may be registered while a host mutation owns the lock, but it
        is not considered active—and executes no run code—until it wins this
        barrier. Whichever side wins first therefore has an unambiguous head.
        """

        build_run = self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES
        assert self._run_execution is not None, "run collaborators not yet bound"
        try:
            if build_run:
                async with self.lock(conversation_id):
                    async with self.interprocess_mutation_fence(conversation_id):
                        if expected_run_intent_id is not None:
                            events = await self._store.get_events(conversation_id)
                            latest_intent = latest_workspace_run_intent(events)
                            if latest_intent is None or latest_intent.id != expected_run_intent_id:
                                raise WorkspaceRunSuperseded(
                                    "registered run intent changed before admission"
                                )
                        await self._build_platform.record_route_locked(conversation_id)
                        await self.record_mutation_locked(
                            conversation_id,
                            "agent.run-claimed",
                        )
                        self._fences.mark_run_admitted(conversation_id)
            return await self._run_execution.run(conversation_id, loop)
        finally:
            if build_run:
                self.clear_run_claim(conversation_id)

    def claim_registered_run_locked(self, conversation_id: str) -> None:
        """Publish an ingress-first run claim before releasing its workspace fence."""

        task = self._ownership.active_task(conversation_id)
        self._fences.mark_registered_run_locked(
            conversation_id,
            active=task is not None,
        )

    def clear_run_claim(self, conversation_id: str) -> None:
        self._fences.clear_run_claim(conversation_id)

    def has_admitted_run(self, conversation_id: str) -> bool:
        return self._fences.has_admitted_run(conversation_id)

    def has_run_claim(self, conversation_id: str) -> bool:
        return self._fences.has_run_claim(conversation_id)

    @asynccontextmanager
    async def _lifecycle_fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Expose only serialization; lifecycle policy stays in its command service."""

        async with self._fences._lifecycle_fence(conversation_id):
            yield

    @asynccontextmanager
    async def _lifecycle_locked_fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Extend a caller-owned local lock across the process fence."""

        async with self._fences._lifecycle_locked_fence(conversation_id):
            yield

    def _require_lifecycle_fence(self, conversation_id: str) -> None:
        """Fail closed unless a Build transition owns both workspace fences."""

        self._fences._require_lifecycle_fence(conversation_id)

    async def append_status(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Compatibility delegate; the lifecycle service owns authorization."""

        return await self._lifecycle_commands.append_status(conversation_id, event)

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Compatibility delegate; the lifecycle service owns authorization."""

        return await self._lifecycle_commands.append_status_locked(
            conversation_id,
            event,
        )

    async def run_authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        """Compatibility delegate for durable lifecycle authority."""

        return await self._lifecycle_commands.authority_is_current(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def _run_authority_is_current_locked(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        return await self._lifecycle_commands._authority_is_current_locked(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def append_run_status_if_current(
        self,
        conversation_id: str,
        event: StatusEvent,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> StatusEvent | None:
        """Compatibility delegate for an atomic task transition."""

        return await self._lifecycle_commands.append_task_status_if_current(
            conversation_id,
            event,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def append_current_run_transition(
        self,
        conversation_id: str,
        events: list[Event],
        status: StatusEvent,
        *,
        expected_statuses: frozenset[ConversationStatus],
    ) -> list[Event] | None:
        """Compatibility delegate for a host lifecycle decision."""

        return await self._lifecycle_commands.append_current_run_transition(
            conversation_id,
            events,
            status,
            expected_statuses=expected_statuses,
        )

    @asynccontextmanager
    async def mutation(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> AsyncIterator[None]:
        """Fence a host edit before yielding the serialized workspace."""

        async with self.fence(conversation_id):
            await self.record_mutation_locked(conversation_id, operation, paths=paths)
            yield

    @asynccontextmanager
    async def fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Serialize a caller that must derive its mutation paths while locked."""

        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                yield

    async def record_mutation_locked(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> WorkspaceMutationEvent:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("workspace mutation fence requires the conversation lock")
        mutation = WorkspaceMutationEvent(
            operation=operation,
            paths=tuple(sorted(set(paths))),
            run_protocol_version=(1 if operation.startswith("agent.run-intent.") else None),
        )
        state = await self._store.get_state(conversation_id)
        interrupts_running_view = not operation.startswith("agent.") and (
            state.execution_status
            in {
                ConversationStatus.RUNNING,
                ConversationStatus.WAITING_FOR_CONFIRMATION,
                ConversationStatus.AWAITING_PLAN_APPROVAL,
                ConversationStatus.AWAITING_USER_DECISION,
                ConversationStatus.AWAITING_USER_QUESTION,
            }
        )
        if interrupts_running_view:
            history = await self._store.get_events(conversation_id)
            pending: list[Event] = [*self._dangling_action_closures(history), mutation]
            pending.append(
                WorkspaceMutationEvent(
                    operation="agent.run-intent.host-mutation",
                    run_protocol_version=1,
                )
            )
            stored_events = await self._store.append_many(conversation_id, pending)
            stored = next(event for event in stored_events if event.id == mutation.id)
            # A host edit is a new input to an active build. Start or hand off a
            # task while the fence is still held; execution itself queues behind
            # the completed host mutation.
            assert self._run_controller is not None, "run collaborators not yet bound"
            self._run_controller.kick(conversation_id)
            self.claim_registered_run_locked(conversation_id)
        else:
            stored = await self._store.append(conversation_id, mutation)
        if not isinstance(stored, WorkspaceMutationEvent):
            raise RuntimeError("event store returned wrong workspace mutation event type")
        return stored

    @staticmethod
    def _dangling_action_closures(history: list[Event]) -> list[AgentErrorEvent]:
        resolved = {
            event.action_id
            for event in history
            if isinstance(event, (ObservationEvent, AgentErrorEvent))
            and event.action_id is not None
        }
        return [
            AgentErrorEvent(
                error="execution_superseded",
                detail=(
                    "A newer user or host instruction arrived before this proposed action "
                    "crossed the workspace effect boundary. The action was not executed."
                ),
                action_id=action.id,
                tool_call_id=action.tool_call.call_id,
                agent_view_id=action.agent_view_id,
            )
            for action in history
            if isinstance(action, ActionEvent) and action.id not in resolved
        ]

    async def record_run_intent_locked(
        self,
        conversation_id: str,
        source: str,
    ) -> WorkspaceMutationEvent | None:
        """Durably invalidate an old workspace seal before a Build execution ingress."""

        if self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES:
            return None
        self._require_process_fence_locked(conversation_id)
        return await self.record_mutation_locked(
            conversation_id,
            f"agent.run-intent.{source}",
        )

    async def append_run_ingress_locked(
        self,
        conversation_id: str,
        events: list[Event],
        source: str,
        *,
        intent: WorkspaceMutationEvent | None = None,
    ) -> list[Event]:
        """Atomically append one user ingress and its durable execution intent."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("run ingress requires the conversation lock")
        history = await self._store.get_events(conversation_id)
        closures = self._dangling_action_closures(history)
        pending = [*closures, *events]
        if self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES:
            self._require_process_fence_locked(conversation_id)
            expected_operation = f"agent.run-intent.{source}"
            if intent is not None and (
                intent.operation != expected_operation
                or intent.run_protocol_version != 1
                or intent.seq is not None
            ):
                raise ValueError("preconstructed run intent does not match ingress")
            pending.append(
                intent
                or WorkspaceMutationEvent(
                    operation=expected_operation,
                    run_protocol_version=1,
                )
            )
        elif intent is not None:
            raise ValueError("preconstructed run intent requires a Build-like surface")
        return await self._append_ingress_events_locked(conversation_id, pending)

    async def _append_ingress_events_locked(
        self,
        conversation_id: str,
        pending: list[Event],
    ) -> list[Event]:
        """Persist an ingress batch through lifecycle only when it has status."""

        if not any(isinstance(event, StatusEvent) for event in pending):
            return await self._store.append_many(conversation_id, pending)
        ingress_intent = next(
            (
                event
                for event in pending
                if isinstance(event, WorkspaceMutationEvent)
                and event.run_protocol_version == 1
                and event.operation.startswith("agent.run-intent.")
            ),
            None,
        )
        return await self._lifecycle_commands.append_transition_batch_locked(
            conversation_id,
            pending,
            ingress_intent=ingress_intent,
        )

    async def rekick_stranded_followup(
        self,
        conversation_id: str,
        claimed_user_seq: int,
    ) -> None:
        """Publish a post-terminal re-kick and its local claim as one fenced ingress."""

        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                await self.record_run_intent_locked(conversation_id, "stranded-followup")
                assert self._run_controller is not None, "run collaborators not yet bound"
                self._run_controller.kick(
                    conversation_id,
                    claimed_user_seq=claimed_user_seq,
                )
                self.claim_registered_run_locked(conversation_id)

    def finish_sealability_probe(
        self,
        conversation_id: str,
    ) -> Callable[[], Awaitable[SealabilityProbeResult]]:
        """Bind one loop's REL-27 probe (lifecycle-resolved: same seam as the seal)."""

        async def probe() -> SealabilityProbeResult:
            return await self._lifecycle.probe_finish_sealability(conversation_id)

        return probe

    def terminal_commit_hook(
        self,
        conversation_id: str,
    ) -> Callable[[StatusEvent], Awaitable[StatusEvent]]:
        """Compatibility delegate for Core's private lifecycle sink."""

        return self._lifecycle_commands.terminal_commit_hook(
            conversation_id,
            authority_provider=lambda: self._ownership.current_lifecycle_task_authority(
                conversation_id
            ),
        )

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
        """Seal host-owned mirror bytes without reading or snapshotting a sandbox.

        The caller must already hold the permanent per-conversation lock. This is
        intentionally distinct from :meth:`finalize_sandbox_change`: server-owned
        files (for example deployment audit records) live in the host mirror and
        must never be overwritten by a stale sandbox snapshot during publication.
        """

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
            except Exception as exc:  # noqa: BLE001 — restored bytes exist but are unsealed
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
                except Exception:  # noqa: BLE001 — F-28: dying-session race
                    # The session was acquired moments ago but a post-terminal
                    # teardown can kill it mid-apply (pilot 620108: FINISHED
                    # +1.7s, apply died, bare 503, nothing logged, and the
                    # identical replay succeeded). Reacquire ONCE through the
                    # existing wake/create chain — never the same object — and
                    # re-run the idempotent apply from the immutable stage.
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

    async def forget(self, conversation_id: str) -> None:
        await self._ownership.cancel_registered_task(conversation_id)
        async with self.lock(conversation_id):
            await self.forget_locked(conversation_id)

    async def forget_locked(self, conversation_id: str) -> None:
        # A task cancelled before its coroutine first executes cannot reach the
        # run-admission finally block or the runtime's done callback.  Clear its
        # synchronous ingress claim explicitly so a reused conversation id is
        # not rejected forever.
        self.clear_run_claim(conversation_id)
        await self._ownership.dispose(conversation_id)

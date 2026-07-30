"""Per-conversation workspace coordination owned outside the runtime facade.

The coordinator owns the permanent lock registry and the operations that must
share that serialization domain: host mutation fences, terminal commit hooks,
version restore, and conversation disposal.  It intentionally reaches the
runtime through a back-reference instead of importing ``runtime`` or
``lifecycle`` so the dependency graph remains acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

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
from disco.tools.sandbox._container import PREVIEW_PORT

from .lifecycle_command_service import LifecycleCommandService
from .workspace_commit import (
    CommittedWorkspaceView,
    WorkspaceCommitUnavailable,
    WorkspaceRunSuperseded,
    resolve_committed_workspace,
)
from .workspace_process_fence import (
    workspace_process_fence,
    workspace_process_fence_held,
)


class WorkspaceRestoreConflict(ValueError):
    """The workspace cannot be restored while a conversation is running."""


class WorkspaceVersionNotFound(LookupError):
    """The requested workspace version does not exist or is unverifiable."""


class WorkspaceRestoreStorageError(RuntimeError):
    """Workspace restore failed because storage or sandbox I/O failed."""


_LOG = logging.getLogger(__name__)


def _current_lifecycle_task_authority(
    rt: Any,
    conversation_id: str,
) -> tuple[str | None, str | None]:
    """Return only the exact registered task's captured durable target."""

    task = asyncio.current_task()
    if task is None or rt._tasks.get(conversation_id) is not task:
        return None, None
    return rt._run_task_authorities.get(task, (None, None))


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


_OWNED_WORKSPACE_FENCES: ContextVar[frozenset[tuple[str, asyncio.Task[Any]]]] = ContextVar(
    "disco_owned_workspace_fences", default=frozenset()
)


class WorkspaceCoordinator:
    """Own the permanent workspace lock and all cross-surface coordination."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt
        # Never remove an entry. Replacing a lock after forget/recreate would
        # split waiters across two coordination domains (an ABA race).
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_runs: set[str] = set()
        self._admitted_runs: set[str] = set()

    def lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def _process_fence_workspace(self, conversation_id: str) -> Path | None:
        store = self._rt._project_store_now()
        if store is not None:
            # Lock identity must not change merely because the projects volume
            # is temporarily unwritable or unavailable. ``path_for`` is a pure,
            # validated identity derivation and does not require the workspace
            # directory to exist, so the same configured root remains the
            # coordination key in healthy and degraded states.
            return store.path_for(conversation_id)
        # In-memory/test stores have no shared cross-process event log. Keep a
        # stable identity within this process rather than silently dropping the
        # fence entirely; production runtimes always take the ProjectStore path.
        store_key = hashlib.sha256(f"{id(self._rt._store)}:{conversation_id}".encode()).hexdigest()
        return Path(tempfile.gettempdir()) / "disco-ephemeral-workspaces" / store_key

    @asynccontextmanager
    async def interprocess_mutation_fence(
        self,
        conversation_id: str,
        *,
        wait: bool = True,
    ) -> AsyncIterator[None]:
        """Extend the caller-owned local fence across Agent server processes."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("process workspace fence requires the conversation lock")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("process workspace fence requires an asyncio task")
        workspace = self._process_fence_workspace(conversation_id)
        token = _OWNED_WORKSPACE_FENCES.set(
            _OWNED_WORKSPACE_FENCES.get() | {(conversation_id, owner)}
        )
        try:
            if workspace is None:
                yield
                return
            async with workspace_process_fence(workspace, wait=wait):
                yield
        finally:
            _OWNED_WORKSPACE_FENCES.reset(token)

    @staticmethod
    def fence_owned_by_current_task(conversation_id: str) -> bool:
        owner = asyncio.current_task()
        return owner is not None and (conversation_id, owner) in _OWNED_WORKSPACE_FENCES.get()

    def _require_process_fence_locked(self, conversation_id: str) -> None:
        workspace = self._process_fence_workspace(conversation_id)
        if workspace is not None and not workspace_process_fence_held(workspace):
            raise RuntimeError("durable run intent requires the workspace process fence")

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

        build_run = self._rt._surface_of(conversation_id) in self._rt._BUILD_LIKE_SURFACES
        try:
            if build_run:
                async with self.lock(conversation_id):
                    async with self.interprocess_mutation_fence(conversation_id):
                        if expected_run_intent_id is not None:
                            events = await self._rt._store.get_events(conversation_id)
                            latest_intent = latest_workspace_run_intent(events)
                            if latest_intent is None or latest_intent.id != expected_run_intent_id:
                                raise WorkspaceRunSuperseded(
                                    "registered run intent changed before admission"
                                )
                        await self._rt._build_platform.record_route_locked(conversation_id)
                        await self.record_mutation_locked(
                            conversation_id,
                            "agent.run-claimed",
                        )
                        self._pending_runs.discard(conversation_id)
                        self._admitted_runs.add(conversation_id)
            return await self._rt._run_with_persistence(conversation_id, loop)
        finally:
            if build_run:
                self.clear_run_claim(conversation_id)

    def claim_registered_run_locked(self, conversation_id: str) -> None:
        """Publish an ingress-first run claim before releasing its workspace fence."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("run claim requires the workspace fence")
        task = self._rt._tasks.get(conversation_id)
        if task is not None and not task.done() and conversation_id not in self._admitted_runs:
            self._pending_runs.add(conversation_id)

    def clear_run_claim(self, conversation_id: str) -> None:
        self._pending_runs.discard(conversation_id)
        self._admitted_runs.discard(conversation_id)

    def has_admitted_run(self, conversation_id: str) -> bool:
        return conversation_id in self._admitted_runs

    def has_run_claim(self, conversation_id: str) -> bool:
        return conversation_id in self._pending_runs or self.has_admitted_run(conversation_id)

    @asynccontextmanager
    async def _lifecycle_fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Expose only serialization; lifecycle policy stays in its command service."""

        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            yield
            return
        if self.fence_owned_by_current_task(conversation_id):
            yield
            return
        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                yield

    @asynccontextmanager
    async def _lifecycle_locked_fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Extend a caller-owned local lock across the process fence."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("locked lifecycle transition requires the workspace fence")
        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            yield
            return
        if self.fence_owned_by_current_task(conversation_id):
            yield
            return
        async with self.interprocess_mutation_fence(conversation_id):
            yield

    def _require_lifecycle_fence(self, conversation_id: str) -> None:
        """Fail closed unless a Build transition owns both workspace fences."""

        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            return
        if not self.lock(conversation_id).locked():
            raise RuntimeError("locked status append requires the workspace fence")
        self._require_process_fence_locked(conversation_id)

    async def append_status(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Compatibility delegate; the lifecycle service owns authorization."""

        return await self._rt._lifecycle_commands.append_status(conversation_id, event)

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Compatibility delegate; the lifecycle service owns authorization."""

        return await self._rt._lifecycle_commands.append_status_locked(
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

        return await self._rt._lifecycle_commands.authority_is_current(
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
        return await self._rt._lifecycle_commands._authority_is_current_locked(
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

        return await self._rt._lifecycle_commands.append_task_status_if_current(
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

        return await self._rt._lifecycle_commands.append_current_run_transition(
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
        state = await self._rt._store.get_state(conversation_id)
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
            history = await self._rt._store.get_events(conversation_id)
            pending: list[Event] = [*self._dangling_action_closures(history), mutation]
            pending.append(
                WorkspaceMutationEvent(
                    operation="agent.run-intent.host-mutation",
                    run_protocol_version=1,
                )
            )
            stored_events = await self._rt._store.append_many(conversation_id, pending)
            stored = next(event for event in stored_events if event.id == mutation.id)
            # A host edit is a new input to an active build. Start or hand off a
            # task while the fence is still held; execution itself queues behind
            # the completed host mutation.
            self._rt.kick(conversation_id)
            self.claim_registered_run_locked(conversation_id)
        else:
            stored = await self._rt._store.append(conversation_id, mutation)
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

        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
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
        history = await self._rt._store.get_events(conversation_id)
        closures = self._dangling_action_closures(history)
        pending = [*closures, *events]
        if self._rt._surface_of(conversation_id) in self._rt._BUILD_LIKE_SURFACES:
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
            return await self._rt._store.append_many(conversation_id, pending)
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
        return await self._rt._lifecycle_commands.append_transition_batch_locked(
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
                self._rt.kick(conversation_id, claimed_user_seq=claimed_user_seq)
                self.claim_registered_run_locked(conversation_id)

    def finish_sealability_probe(
        self,
        conversation_id: str,
    ) -> Callable[[], Awaitable[SealabilityProbeResult]]:
        """Bind one loop's REL-27 probe (lifecycle-resolved: same seam as the seal)."""

        async def probe() -> SealabilityProbeResult:
            return await self._rt._lifecycle.probe_finish_sealability(conversation_id)

        return probe

    def terminal_commit_hook(
        self,
        conversation_id: str,
    ) -> Callable[[StatusEvent], Awaitable[StatusEvent]]:
        """Compatibility delegate for Core's private lifecycle sink."""

        return self._rt._lifecycle_commands.terminal_commit_hook(
            conversation_id,
            authority_provider=lambda: _current_lifecycle_task_authority(self._rt, conversation_id),
        )

    async def _host_mutation_authority(
        self,
        conversation_id: str,
        operation: str,
    ) -> str:
        events = await self._rt._store.get_events(conversation_id)
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
        await self._rt._lifecycle_commands.commit_finished_workspace(
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
        await self._rt._lifecycle_commands._commit_finished_host_mirror_locked(
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
        state = await self._rt._store.get_state(conversation_id)
        if state.execution_status is not ConversationStatus.FINISHED:
            raise WorkspaceCommitUnavailable("workspace is not at an inactive FINISHED head")
        events = await self._rt._store.get_events(conversation_id)
        committed = resolve_committed_workspace(
            events,
            self._rt._project_store_now(),
            conversation_id,
        )
        facts = self._rt._project_store_now().inspect_workspace(conversation_id)
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
        events = await self._rt._store.get_events(conversation_id)
        try:
            return resolve_committed_workspace(
                events,
                self._rt._project_store_now(),
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
                prior_state = await self._rt._store.get_state(conversation_id)
                result = await self.restore_version_locked(conversation_id, seq)
        if prior_state.execution_status is ConversationStatus.FINISHED:
            try:
                committed = await self._rt.finalize_host_workspace_change(
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
        state = await self._rt._store.get_state(conversation_id)
        if state.execution_status is ConversationStatus.RUNNING:
            raise WorkspaceRestoreConflict("conversation is running")

        store = self._rt._project_store_now()
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

        session = await self._restore_session(conversation_id)
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
                    fresh = await self._restore_session(conversation_id, exclude=session)
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

    async def _restore_session(self, conversation_id: str, *, exclude: Any = None) -> Any:
        """Acquire a session for restore; ``exclude`` marks one that just
        failed (F-28) — the registry may still return that dying object, and
        handing it back would retry a dead container as if it were fresh."""

        def _usable(candidate: Any) -> Any:
            return None if candidate is None or candidate is exclude else candidate

        session = _usable(self._rt.live_session(conversation_id))
        if session is None:
            cid8 = conversation_id.removeprefix("conv_")[:8]
            with contextlib.suppress(Exception):
                await self._rt.wake_for_preview(cid8, PREVIEW_PORT)
            session = _usable(self._rt.live_session(conversation_id))
        if session is None:
            try:
                self._rt._loop_for(conversation_id)
            except Exception as exc:  # noqa: BLE001 — mapped to a named storage error
                _LOG.error("could not create sandbox for restore of %s: %s", conversation_id, exc)
                raise WorkspaceRestoreStorageError(
                    f"could not create sandbox for restore: {exc}"
                ) from exc
            session = _usable(self._rt.live_session(conversation_id))
        if session is None:
            _LOG.error("sandbox unavailable for restore of %s", conversation_id)
            raise WorkspaceRestoreStorageError("sandbox unavailable for restore")
        return session

    async def _append_restore_events(
        self,
        conversation_id: str,
        seq: int,
        tree_digest: str,
        label: str,
    ) -> None:
        await self._rt._store.append(
            conversation_id,
            WorkspaceRestoredEvent(
                version_seq=seq,
                tree_digest=tree_digest,
                label=label,
            ),
        )
        await self._rt._store.append(
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
        self._rt._clear_pinned_kernel(conversation_id)
        task = self._rt._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with self.lock(conversation_id):
            await self.forget_locked(conversation_id)

    async def forget_locked(self, conversation_id: str) -> None:
        # A task cancelled before its coroutine first executes cannot reach the
        # run-admission finally block or the runtime's done callback.  Clear its
        # synchronous ingress claim explicitly so a reused conversation id is
        # not rejected forever.
        self.clear_run_claim(conversation_id)
        executor = self._rt._executors.pop(conversation_id, None)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()
        session = self._rt._pending_sessions.pop(conversation_id, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.destroy()
        with contextlib.suppress(Exception):
            await self._rt._sandbox_service_now().destroy_by_conversation(conversation_id)
        for cache in self._conversation_caches():
            cache.pop(conversation_id, None)
        self._rt._contract_fold_attempted.discard(conversation_id)
        self._rt._driver_proven = {
            proven for proven in self._rt._driver_proven if proven[0] != conversation_id
        }

    def _conversation_caches(self) -> tuple[dict, ...]:
        return (
            self._rt._run_generation,
            self._rt._loops,
            self._rt._nonterminal_rekicks,
            self._rt._last_rekick_progress_seq,
            self._rt._post_terminal_rekick_seq,
            self._rt._run_claimed_user_seq,
            self._rt._last_status,
            self._rt._cancel_flags,
            self._rt._model_override,
            self._rt._surface,
            self._rt._autonomous,
            self._rt._assist,
            self._rt._quiet,
            self._rt._artifact_mode,
            self._rt._appkit_mode,
            self._rt._depth,
            self._rt._dr_steer,
            self._rt._dr_injected_sources,
            self._rt._upload_passages,
            self._rt._last_sessions,
            self._rt._mcp_approval_pending,
            self._rt._build_kind,
            self._rt._build_trackers,
            self._rt._build_audit_trackers,
            self._rt._toolscope_audits,
        )

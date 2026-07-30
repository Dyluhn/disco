"""Concrete narrow ports for sandbox lifecycle dependencies.

Each port is a cohesive typed owner that exposes only the methods the lifecycle
collaborators need.  No port mirrors the runtime, bundles callables, or acts as
a service locator.  The integrator constructs these ports from the already-split
subowners and passes them to the lifecycle collaborators.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from disco.core import EventFilter
from disco.core.llm import ConfigStore
from disco.core.loop import SealabilityProbeResult
from disco.tools.projects import ProjectStore

from .connection_tracker import ConnectionTracker
from .persistence_notifier import PersistenceNotifier
from .project_runtime_service import ProjectRuntimeService
from .run_registry import KernelPinRegistry, LoopRegistry, RunRegistry, RunResourceRegistry
from .run_stranded_sweep import RunStrandedSweep
from .sandbox_runtime_service import SandboxRuntimeService
from .upload_store import UploadStore
from .workspace_persistence import WorkspacePersistence, probe_finish_sealability

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

_DEFAULT_IDLE_SWEEP_INTERVAL_S = 60.0
_MIN_IDLE_SWEEP_INTERVAL_S = 5.0


class LifecycleStoreAccess:
    """Narrow the event store to the reads lifecycle sweeps and suspend need."""

    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store

    async def list_conversations(
        self,
        *,
        owner_id: str,
        limit: int,
        cursor: str | None,
    ) -> list[str]:
        return await self._store.list_conversations(
            owner_id=owner_id, limit=limit, cursor=cursor
        )

    async def get_state(self, conversation_id: str):
        return await self._store.get_state(conversation_id)

    async def get_events(
        self,
        conversation_id: str,
        *,
        event_filter: EventFilter | None = None,
    ):
        if event_filter is not None:
            return await self._store.get_events(conversation_id, event_filter)
        return await self._store.get_events(conversation_id)

    async def conversation_exists(self, conversation_id: str) -> bool:
        return await self._store.conversation_exists(conversation_id)


class LifecycleRunState:
    """Combine run registry, resource registry, and loop registry for lifecycle.

    Exposes the run-state queries and mutations that ``GateReaper``,
    ``OrphanReconciler``, ``Rehydration``, and ``LifecycleManager`` need without
    giving them a back-reference to the runtime.
    """

    def __init__(
        self,
        runs: RunRegistry,
        resources: RunResourceRegistry,
        loops: LoopRegistry,
    ) -> None:
        self._runs = runs
        self._resources = resources
        self._loops = loops

    # -- RunRegistry ---------------------------------------------------------

    def active_task(self, conversation_id: str):
        return self._runs.active_task(conversation_id)

    def active_conversation_ids(self) -> tuple[str, ...]:
        return self._runs.active_conversation_ids()

    def generation(self, conversation_id: str) -> int | None:
        return self._runs.generation(conversation_id)

    def generation_is_current(
        self,
        conversation_id: str,
        generation: int | None,
    ) -> bool:
        return self._runs.generation_is_current(conversation_id, generation)

    def task(self, conversation_id: str):
        return self._runs.task(conversation_id)

    def detach_task_if_owned(self, conversation_id: str, task) -> bool:
        return self._runs.detach_task_if_owned(conversation_id, task)

    def restore_task_if_generation(
        self,
        conversation_id: str,
        task,
        generation: int | None,
    ) -> bool:
        return self._runs.restore_task_if_generation(conversation_id, task, generation)

    # -- RunResourceRegistry -------------------------------------------------

    def executor(self, conversation_id: str):
        return self._resources.executor(conversation_id)

    def has_executor(self, conversation_id: str) -> bool:
        return self._resources.has_executor(conversation_id)

    def pop_executor(self, conversation_id: str):
        return self._resources.pop_executor(conversation_id)

    def pending_session(self, conversation_id: str):
        return self._resources.pending_session(conversation_id)

    def has_pending_session(self, conversation_id: str) -> bool:
        return self._resources.has_pending_session(conversation_id)

    def pop_pending_session(self, conversation_id: str):
        return self._resources.pop_pending_session(conversation_id)

    def conversation_ids(self, *, executors_only: bool = False) -> tuple[str, ...]:
        return self._resources.conversation_ids(executors_only=executors_only)

    def resources_registry(self) -> RunResourceRegistry:
        """Expose the backing resource registry for the seal-probe adapter."""
        return self._resources

    # -- LoopRegistry --------------------------------------------------------

    def loop(self, conversation_id: str):
        return self._loops.loop(conversation_id)

    def forget(self, conversation_id: str) -> None:
        self._loops.forget(conversation_id)


class LifecycleKernelPins:
    """Generation-safe kernel-pin clearing for the gate reaper."""

    def __init__(self, pins: KernelPinRegistry, runs: RunRegistry) -> None:
        self._pins = pins
        self._runs = runs

    def clear_if_current(
        self,
        conversation_id: str,
        generation: int | None,
    ) -> None:
        self._pins.clear_if_current(conversation_id, generation, self._runs)


class LifecycleSandboxAccess:
    """Narrow sandbox service and project store resolution for lifecycle."""

    def __init__(
        self,
        sandbox: SandboxRuntimeService,
        projects: ProjectRuntimeService,
    ) -> None:
        self._sandbox = sandbox
        self._projects = projects

    def sandbox_service_now(self):
        return self._sandbox._sandbox_service_now()

    def current_project_store(self) -> ProjectStore:
        return self._projects.current_project_store()


class LifecycleConnections:
    """Narrow connection tracker for lifecycle connect/disconnect/suspend."""

    def __init__(self, connections: ConnectionTracker) -> None:
        self._connections = connections

    def has_connections(self, conversation_id: str) -> bool:
        return self._connections.has_connections(conversation_id)

    def clear_session_state(self, conversation_id: str) -> None:
        self._connections.clear_session_state(conversation_id)

    def on_connect(self, conversation_id: str) -> None:
        self._connections.on_connect(conversation_id)

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        self._connections.on_disconnect(conversation_id, grace_s=grace_s)

    async def suspend_after_grace(
        self,
        conversation_id: str,
        grace_s: float,
    ) -> None:
        await self._connections._suspend_after_grace(conversation_id, grace_s)


class LifecycleIdleSweepDeps:
    """Idle-sweep-loop dependencies: stranded run sweep and idle TTL config."""

    def __init__(
        self,
        run_sweep: RunStrandedSweep,
        config_store: ConfigStore,
    ) -> None:
        self._run_sweep = run_sweep
        self._config_store = config_store

    async def sweep_stranded_once(self) -> None:
        await self._run_sweep.sweep_once()

    def idle_ttl_s(self) -> float:
        return float(self._config_store.load().sandbox.idle_ttl_s)


class LifecycleUploads:
    """Narrow upload store for rehydration."""

    def __init__(self, uploads: UploadStore) -> None:
        self._uploads = uploads

    def directory(self, conversation_id: str) -> Path | None:
        return self._uploads.directory(conversation_id)


class LifecyclePersistenceNotifier:
    """Narrow persistence notifier for rehydration failure reporting."""

    def __init__(self, notifier: PersistenceNotifier) -> None:
        self._notifier = notifier

    async def emit(self, conversation_id: str, message: str) -> None:
        await self._notifier.emit(conversation_id, message)


class _SealProbeRuntime:
    """Minimal adapter for the module-level ``probe_finish_sealability``.

    The probe function in ``workspace_persistence`` (not allowlisted for this
    package) accepts a runtime-typed argument and reads only its
    ``_run_resources.executor``.  This adapter exposes exactly that one
    attribute — it is not a runtime mirror, a dependency dataclass, or a
    service locator.
    """

    def __init__(self, resources: RunResourceRegistry) -> None:
        self._run_resources = resources


class WorkspaceSealProbe:
    """Adapt the sealability probe to the run-resource owner."""

    def __init__(self, resources: RunResourceRegistry) -> None:
        self._runtime = _SealProbeRuntime(resources)

    async def probe(
        self,
        conversation_id: str,
        *,
        snapshot_fn,
    ) -> SealabilityProbeResult:
        return await probe_finish_sealability(
            self._runtime,
            conversation_id,
            snapshot_fn=snapshot_fn,
        )


class LifecyclePersistenceOwner:
    """Expose the persistence collaborator's committed-workspace operations.

    ``WorkspacePersistence`` itself still receives the runtime (it lives outside
    this package's allowlist); this port narrows the two methods
    ``LifecycleManager`` delegates to it.
    """

    def __init__(self, persistence: WorkspacePersistence) -> None:
        self._persistence = persistence

    async def recover_finalization_journals(self) -> int:
        return await self._persistence._do_recover_finalization_journals()

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event,
        *,
        require_inactive_finished_head: bool = False,
        snapshot_fn,
    ):
        return await self._persistence._do_commit_finished_workspace(
            conversation_id,
            terminal_event,
            require_inactive_finished_head=require_inactive_finished_head,
            snapshot_fn=snapshot_fn,
        )

    async def commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event,
    ):
        return await self._persistence._do_commit_finished_host_mirror_locked(
            conversation_id,
            terminal_event,
        )

    async def capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict | None = None,
        pinned_session=None,
        snapshot_fn,
    ):
        return await self._persistence._do_capture_workspace(
            conversation_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            journal=journal,
            snapshot_fn=snapshot_fn,
            pinned_session=pinned_session,
        )

    async def maybe_synthesize_app_deliverable(
        self,
        conversation_id: str,
        snapshot_dir: Path,
    ) -> None:
        return await self._persistence._do_maybe_synthesize_app_deliverable(
            conversation_id, snapshot_dir
        )

    @staticmethod
    def find_snapshot_index(snapshot_dir: Path) -> Path | None:
        return WorkspacePersistence._find_snapshot_index(snapshot_dir)

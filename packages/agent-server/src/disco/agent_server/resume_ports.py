"""Concrete narrow ports for resume-service dependencies.

Each port is a cohesive typed owner that exposes only the methods the resume
collaborators need.  No port mirrors the runtime, bundles callables, or acts
as a service locator.  The integrator constructs these ports from the
already-split subowners and passes them to the resume collaborators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from disco.tools.projects import ProjectStore

from .project_runtime_service import ProjectRuntimeService
from .run_registry import (
    CancellationRegistry,
    LoopRegistry,
    RunRegistry,
    RunResourceRegistry,
)
from .runtime_settings import RuntimeSettings
from .sessions_service import SessionsService
from .upload_store import UploadStore
from .workspace_service import WorkspaceCoordinator

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore


class ResumeStoreAccess:
    """Narrow the event store to the reads resume needs."""

    def __init__(self, store: SqliteEventStore) -> None:
        self._store = store

    async def get_state(self, conversation_id: str):
        return await self._store.get_state(conversation_id)

    async def get_events(self, conversation_id: str):
        return await self._store.get_events(conversation_id)


class ResumeRunState:
    """Combine run registry, resource registry, loop registry, and cancellation.

    Exposes the run-state queries and mutations that ``_ResumeTaskDrainer``,
    ``_ResumeContextReconstructor``, and ``ResumeService`` need without giving
    them a back-reference to the runtime.
    """

    def __init__(
        self,
        runs: RunRegistry,
        resources: RunResourceRegistry,
        loops: LoopRegistry,
        cancellations: CancellationRegistry,
    ) -> None:
        self._runs = runs
        self._resources = resources
        self._loops = loops
        self._cancellations = cancellations

    # -- RunRegistry ---------------------------------------------------------

    def task(self, conversation_id: str):
        return self._runs.task(conversation_id)

    def generation(self, conversation_id: str) -> int | None:
        return self._runs.generation(conversation_id)

    def generation_is_current(
        self,
        conversation_id: str,
        generation: int | None,
    ) -> bool:
        return self._runs.generation_is_current(conversation_id, generation)

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

    def has_executor(self, conversation_id: str) -> bool:
        return self._resources.has_executor(conversation_id)

    # -- LoopRegistry --------------------------------------------------------

    def loop(self, conversation_id: str):
        return self._loops.loop(conversation_id)

    # -- CancellationRegistry ------------------------------------------------

    def clear_cancellation(self, conversation_id: str) -> None:
        self._cancellations.clear(conversation_id)


class ResumeWorkspaceFence:
    """Narrow workspace coordinator to the fence operations resume needs."""

    def __init__(self, workspace: WorkspaceCoordinator) -> None:
        self._workspace = workspace

    def lock(self, conversation_id: str):
        return self._workspace.lock(conversation_id)

    def interprocess_mutation_fence(self, conversation_id: str):
        return self._workspace.interprocess_mutation_fence(conversation_id)

    def clear_run_claim(self, conversation_id: str) -> None:
        self._workspace.clear_run_claim(conversation_id)


class ResumeEnvironmentProbe:
    """Narrow project store, uploads, and sessions for resume reconstruction."""

    def __init__(
        self,
        projects: ProjectRuntimeService,
        uploads: UploadStore,
        sessions: SessionsService,
    ) -> None:
        self._projects = projects
        self._uploads = uploads
        self._sessions = sessions

    def current_project_store(self) -> ProjectStore:
        return self._projects.current_project_store()

    def upload_names(self, conversation_id: str) -> set[str]:
        return self._uploads.names(conversation_id)

    async def sessions_snapshot(self, conversation_id: str):
        return await self._sessions.sessions_snapshot(conversation_id)


class ResumeSurfaceSettings:
    """Narrow runtime settings to the surface classification resume needs."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def surface_of(self, conversation_id: str) -> str:
        return self._settings._surface_of(conversation_id)


class RunStartPort(Protocol):
    """The bounded run-start boundary.

    ``ResumeService`` triggers the pinned run start through this port after the
    atomic transition append.  The concrete adapter (``PinnedRunStart``) wraps
    the kernel-pin registry and is constructed after the kernel pins are
    available — breaking the resume-start circularity without a post-construction
    service locator.
    """

    def start(self, conversation_id: str) -> None: ...


class PinnedRunStart:
    """Start a run through the pinned kernel — the bounded run-start boundary.

    Replicates ``ConversationControlService.start`` exactly: ensure the kernel
    pin, call ``kernel.start``, and clear a newly-created pin on failure.  The
    integrator constructs this after ``KernelPinRegistry`` is available and
    passes it to ``ResumeService``, breaking the resume-start circularity.
    """

    def __init__(self, pins) -> None:
        self._pins = pins

    def start(self, conversation_id: str) -> None:
        newly_pinned = self._pins.current(conversation_id) is None
        kernel = self._pins.ensure(conversation_id)
        try:
            kernel.start(conversation_id)
        except BaseException:
            if newly_pinned:
                self._pins.clear(conversation_id)
            raise

"""Single public workspace port composed from private cohesive operations.

Only this coordinator holds collaborators.  Its bases organize behavior while
sharing the same fence registry, stores, revision source, and run resources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .lifecycle_command_service import LifecycleCommandService
from .workspace_fence import WorkspaceFenceService
from .workspace_ownership import WorkspaceOwnership
from .workspace_revisions import (
    WorkspaceRestoreConflict,
    WorkspaceRestoreStorageError,
    WorkspaceVersionNotFound,
    _WorkspaceRevisions,
)
from .workspace_run_authority import (
    _WorkspaceLifecycleAuthority,
    _WorkspaceRunAuthority,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .build_platform_runtime import BuildPlatformRuntime
    from .lifecycle import LifecycleManager
    from .project_runtime_service import ProjectRuntimeService
    from .run_controller import RunController
    from .run_supervisor import RunPersistenceSupervisor
    from .runtime_settings import RuntimeSettings


class WorkspaceCoordinator(
    _WorkspaceRunAuthority,
    _WorkspaceLifecycleAuthority,
    _WorkspaceRevisions,
):
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
        """Bind typed run collaborators after the composition root wires them."""

        self._run_controller = run_controller
        self._run_execution = run_execution


__all__ = [
    "WorkspaceCoordinator",
    "WorkspaceRestoreConflict",
    "WorkspaceRestoreStorageError",
    "WorkspaceVersionNotFound",
]

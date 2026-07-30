"""Explicit constructor dependencies for ``PreviewService``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disco.core.llm import ConfigStore
    from disco.core.store.sqlite import SqliteEventStore

    from .build_loop_factory import BuildLoopFactory
    from .connection_tracker import ConnectionTracker
    from .lifecycle import LifecycleManager
    from .project_runtime_service import ProjectRuntimeService
    from .run_registry import RunResourceRegistry
    from .runtime_settings import RuntimeSettings
    from .workspace_service import WorkspaceCoordinator


class PreviewServiceDependencies:
    """Store Preview's explicit named owners without a runtime back-reference."""

    def __init__(
        self,
        run_resources: RunResourceRegistry,
        store: SqliteEventStore,
        config_store: ConfigStore,
        settings: RuntimeSettings,
        connections: ConnectionTracker,
        projects: ProjectRuntimeService,
        lifecycle: LifecycleManager,
        loop_factory: BuildLoopFactory,
        workspace: WorkspaceCoordinator,
    ) -> None:
        self._run_resources = run_resources
        self._store = store
        self._config_store = config_store
        self._settings = settings
        self._connections = connections
        self._projects = projects
        self._lifecycle = lifecycle
        self._loop_factory = loop_factory
        self._workspace = workspace

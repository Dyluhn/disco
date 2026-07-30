"""Explicit constructor dependencies for ``WorkspacePersistence``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .artifact_manifest_shadow import ArtifactManifestShadow
    from .persistence_notifier import PersistenceNotifier
    from .project_runtime_service import ProjectRuntimeService
    from .run_registry import RunRegistry, RunResourceRegistry
    from .workspace_fence import WorkspaceFenceService


class WorkspacePersistenceDependencies:
    """Store persistence's explicit named owners without a runtime handle."""

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

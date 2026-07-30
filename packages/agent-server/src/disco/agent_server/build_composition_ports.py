"""Concrete, bounded collaborators for Build composition."""

from __future__ import annotations

from disco.core import WorkspaceVersionEvent
from disco.core.llm import ConfigStore
from disco.tools import DefaultToolExecutor
from disco.tools.projects import ProjectStore
from disco.tools.sandbox import SandboxInstance

from .lifecycle import LifecycleManager
from .runtime_settings import RuntimeSettings


class BuildSurfacePolicy:
    """Classify Build surfaces through the settings owner."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def is_build_like(self, conversation_id: str) -> bool:
        return self._settings._surface_of(conversation_id) in {"build", "agent"}


class BuildExecutorAccess:
    """Expose only route rollback and live-sandbox lookup."""

    def __init__(self, executors: dict[str, DefaultToolExecutor]) -> None:
        self._executors = executors

    def discard_composed_executor(self, conversation_id: str) -> None:
        self._executors.pop(conversation_id, None)

    def executor_for(self, conversation_id: str) -> DefaultToolExecutor | None:
        return self._executors.get(conversation_id)

    def sandbox_for(self, conversation_id: str) -> SandboxInstance | None:
        executor = self._executors.get(conversation_id)
        return executor.sandbox if executor is not None else None


class CurrentProjectStore:
    """Resolve the active project store without exposing configuration state."""

    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store

    def current_project_store(self) -> ProjectStore:
        return ProjectStore(self._config_store.load().projects.projects_root)


class WorkspaceRevisionCapture:
    """Expose the lifecycle owner's fenced revision operation."""

    def __init__(self, lifecycle: LifecycleManager) -> None:
        self._lifecycle = lifecycle

    async def capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str,
    ) -> WorkspaceVersionEvent | None:
        return await self._lifecycle._capture_workspace(
            conversation_id,
            trigger=trigger,
            version_label=version_label,
        )

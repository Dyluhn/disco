"""Current-project resolution and release-intent persistence."""

from __future__ import annotations

from disco.core.llm import ConfigStore
from disco.core.release.spec import ReleaseIntent
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectStore, StorageError, StorageStatus
from disco.tools.reference_packs import ReferencePackWriteError
from disco.tools.release_intent import ReleaseIntentWriteError

from .reference_pack_store import JsonReferencePackStore, ReferencePackError


class ProjectRuntimeService:
    """Resolve the live project root at invocation time and write through it."""

    def __init__(
        self,
        config_store: ConfigStore,
        event_store: SqliteEventStore,
    ) -> None:
        self._config_store = config_store
        self._event_store = event_store

    def current_project_store(self) -> ProjectStore:
        return ProjectStore(self._config_store.load().projects.projects_root)

    async def write_release_intent(
        self,
        conversation_id: str,
        owner_id: str,
        intent: ReleaseIntent,
    ) -> None:
        project_store = self.current_project_store()
        status = project_store.status()
        if status is not StorageStatus.OK:
            raise ReleaseIntentWriteError(
                "invalid_projects_root",
                "the configured projects root is not a writable directory, so the "
                f"release intent was not recorded (root status: {status.value}).",
            )
        conversation_owner = self._event_store.conversation_owner_id_sync(conversation_id)
        if conversation_owner is not None and conversation_owner != owner_id:
            raise ReleaseIntentWriteError(
                "owner_conversation_mismatch",
                "cannot record release intent for a conversation owned by a "
                "different owner; nothing was persisted.",
            )
        try:
            project_store.write_release_intent(conversation_id, intent)
        except (StorageError, OSError) as exc:
            raise ReleaseIntentWriteError(
                "release_intent_persist_failed",
                "failed to persist the release intent under the configured projects "
                "root; nothing was persisted.",
            ) from exc

    async def create_reference_pack(
        self, owner_id: str, name: str, description: str, files: list[tuple[str, bytes]]
    ) -> dict:
        """Copy validated bytes into a new pack under the active projects root."""
        project_store = self.current_project_store()
        status = project_store.status()
        root = project_store.root
        if status is not StorageStatus.OK or root is None:
            raise ReferencePackWriteError(
                "invalid_projects_root",
                "the configured projects root is not a writable directory, so the pack "
                f"was not saved (root status: {status.value}).",
            )
        try:
            record = JsonReferencePackStore(root).create(
                owner_id=owner_id, name=name, description=description, files=files
            )
        except ReferencePackError as exc:
            raise ReferencePackWriteError("invalid_reference_pack", str(exc)) from exc
        except OSError as exc:
            raise ReferencePackWriteError(
                "reference_pack_persist_failed",
                "failed to persist the pack under the configured projects root; nothing was saved.",
            ) from exc
        return record.summary()

"""Workspace rehydration for the sandbox lifecycle domain."""

from __future__ import annotations

import logging

from disco.tools.projects import StorageStatus, rehydrate_workspace

from .lifecycle_ports import (
    LifecyclePersistenceNotifier,
    LifecycleRunState,
    LifecycleSandboxAccess,
    LifecycleUploads,
)

_LOG = logging.getLogger(__name__)


class Rehydration:
    """Restore snapshot files and uploads into a live sandbox."""

    def __init__(
        self,
        sandbox: LifecycleSandboxAccess,
        run_state: LifecycleRunState,
        notifier: LifecyclePersistenceNotifier,
        uploads: LifecycleUploads,
    ) -> None:
        self._sandbox = sandbox
        self._run_state = run_state
        self._notifier = notifier
        self._uploads = uploads
        self._rehydrated: set[str] = set()

    def _mark_rehydrated(self, conversation_id: str) -> None:
        self._rehydrated.add(conversation_id)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        """Restore a prior snapshot once into the current live sandbox."""
        if conversation_id in self._rehydrated:
            return
        self._rehydrated.add(conversation_id)
        store = self._sandbox.current_project_store()
        if store is None or store.status() != StorageStatus.OK:
            return
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — unreadable manifests are not restorable
            return
        if record is None or record.files_missing:
            return
        executor = self._run_state.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        try:
            await rehydrate_workspace(session, store.path_for(conversation_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            await self._notifier.emit(
                conversation_id,
                f"Could not restore project files: {exc}",
            )

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        """Restore bytes and uploads after a mid-run sandbox replacement."""
        self._rehydrated.discard(conversation_id)
        await self._maybe_rehydrate(conversation_id)
        await self._rematerialize_uploads(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        """Copy server-held uploads back into the fresh sandbox."""
        executor = self._run_state.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            session = self._run_state.pending_session(conversation_id)
        if session is None:
            return
        uploads_dir = self._uploads.directory(conversation_id)
        if uploads_dir is None:
            return
        written = 0
        for path in uploads_dir.iterdir():
            if path.is_file():
                try:
                    await session.write_file(f"uploads/{path.name}", path.read_bytes())
                    written += 1
                except Exception:  # noqa: BLE001 — best effort
                    _LOG.warning(
                        "[dc-07] failed to re-materialize upload %r for %s",
                        path.name,
                        conversation_id,
                    )
        if written:
            _LOG.info(
                "[dc-07] re-materialized %d upload(s) for %s",
                written,
                conversation_id,
            )

    def clear(self, conversation_id: str) -> None:
        self._rehydrated.discard(conversation_id)

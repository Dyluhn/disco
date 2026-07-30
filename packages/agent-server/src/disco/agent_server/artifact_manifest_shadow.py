"""Observe-only artifact-manifest projection at terminal persistence."""

from __future__ import annotations

import logging
from typing import Any

from disco.core.context.artifact_projection import (
    artifact_paths_from_events,
    manifest_path_divergence,
)
from disco.core.context.ledger import ArtifactRecord
from disco.core.context.store import ArtifactMemoryStore
from disco.core.store.sqlite import SqliteEventStore

from .run_registry import RunResourceRegistry

_LOG = logging.getLogger(__name__)


class ArtifactManifestShadow:
    """Fold emitted artifact paths into the sandbox manifest, best effort."""

    def __init__(
        self,
        store: SqliteEventStore,
        resources: RunResourceRegistry,
    ) -> None:
        self._store = store
        self._resources = resources

    async def fold(
        self,
        conversation_id: str,
        *,
        events: list[Any] | None = None,
    ) -> None:
        try:
            executor = self._resources.executor(conversation_id)
            sandbox = executor.sandbox if executor is not None else None
            if sandbox is None:
                _LOG.info(
                    "shadow fold skipped for %s: sandbox already released",
                    conversation_id,
                )
                return
            history = events if events is not None else await self._store.get_events(
                conversation_id
            )
            projected = artifact_paths_from_events(history)
            memory = ArtifactMemoryStore(sandbox)
            manifest = await memory.read_artifacts()
            manifest_paths = {record.path for record in manifest}
            missing, extra = manifest_path_divergence(projected, manifest_paths)
            if missing or extra:
                _LOG.info(
                    "artifact-manifest shadow divergence for %s: missing=%s extra=%s",
                    conversation_id,
                    sorted(missing),
                    sorted(extra),
                )
            for path in sorted(missing):
                await memory.upsert_artifact(ArtifactRecord(path=path))
        except Exception:  # noqa: BLE001
            _LOG.exception("artifact-manifest shadow fold failed for %s", conversation_id)

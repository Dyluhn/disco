"""Upload ingestion and artifact lookup mechanics for file routes."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import posixpath
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disco.core import (
    ConversationStatus,
    DatasourceEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from disco.core.context.ledger import ResourceRef
from disco.core.context.store import ArtifactMemoryStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from fastapi import UploadFile

from ..runtime import ConversationRuntime
from ..uploads_ingest import parse_upload_to_doc
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import (
    _ARTIFACT_TYPES,
    _MAX_CONV_BYTES,
    _MAX_FILE_BYTES,
    _declared_artifacts,
    _sanitize_name,
)

_LOG = logging.getLogger(__name__)

_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# CXT-2 resource-import hook: uploads become durable ResourceRefs in the
# resource manifest (the READ side is ContextLedger.resource_manifest → the
# ContextPack "Resources:" section). Same file the ArtifactMemoryStore owns via
# path_for(ArtifactMemoryKind.RESOURCE_MANIFEST); spelled out here so the
# mutation declaration matches workspace_persistence's manifest handling.
_RESOURCE_MANIFEST_PATH = ".disco/context/resource_manifest.json"


async def _fold_upload_resource_ref(
    session: Any,
    conversation_id: str,
    final_name: str,
    data: bytes,
) -> None:
    """Upsert this upload into the durable resource manifest, best effort.

    ``record_resources`` is whole-list-replace, so this reads the current
    manifest, replaces the entry with the same ``rel_path`` (or appends), and
    writes the merged list back. Runs under the caller's workspace fence, which
    serializes manifest read-merge-write per conversation. Manifest bookkeeping
    must never fail an upload: any error is logged and swallowed (matching
    ArtifactManifestShadow's failure posture).
    """
    try:
        memory = ArtifactMemoryStore(session)
        ref = ResourceRef(
            rel_path=f"uploads/{final_name}",
            source=f"upload://{conversation_id}/{final_name}",
            sha256=hashlib.sha256(data).hexdigest(),
            copied_at=datetime.now(UTC),
        )
        merged = list(await memory.read_resources())
        for i, existing in enumerate(merged):
            if existing.rel_path == ref.rel_path:
                merged[i] = ref
                break
        else:
            merged.append(ref)
        await memory.record_resources(tuple(merged))
    except Exception:  # noqa: BLE001 — manifest bookkeeping never fails an upload
        _LOG.exception(
            "resource-manifest fold failed for %s (uploads/%s)",
            conversation_id,
            final_name,
        )


@dataclass(frozen=True)
class UploadBatch:
    saved: list[dict[str, Any]]
    rejected: list[dict[str, Any]]

    @property
    def status_code(self) -> int:
        return 413 if not self.saved and self.rejected else 200

    def payload(self) -> dict[str, list[dict[str, Any]]]:
        return {"saved": self.saved, "rejected": self.rejected}


@dataclass(frozen=True)
class ArtifactPayload:
    data: bytes
    media_type: str
    basename: str


class ArtifactRejected(ValueError):
    def __init__(self, status_code: int, detail: dict[str, str] | None = None) -> None:
        super().__init__((detail or {}).get("reason", "artifact_not_found"))
        self.status_code = status_code
        self.detail = detail


async def _sandbox_upload_usage(
    runtime: ConversationRuntime,
    conversation_id: str,
    session: Any,
) -> tuple[set[str], int]:
    server_names = runtime.uploads.names(conversation_id)
    try:
        sandbox_names: set[str] = set(await session.list_dir("uploads"))
    except Exception:  # noqa: BLE001 — uploads/ may not exist yet
        sandbox_names = set()
    existing_bytes = runtime.uploads.size(conversation_id)
    for filename in sandbox_names - server_names:
        with contextlib.suppress(Exception):
            existing_bytes += len(await session.read_file(f"uploads/{filename}"))
    return server_names | sandbox_names, existing_bytes


def _available_name(clean: str, existing_names: set[str]) -> str:
    stem = Path(clean).stem
    suffix = Path(clean).suffix
    final_name = clean
    counter = 2
    while final_name in existing_names:
        final_name = f"{stem}-{counter}{suffix}"
        counter += 1
    return final_name


async def _store_upload(
    runtime: ConversationRuntime,
    conversation_id: str,
    session: Any,
    clean: str,
    data: bytes,
) -> str | None:
    async with runtime.workspace.fence(conversation_id):
        existing_names, existing_bytes = await _sandbox_upload_usage(
            runtime, conversation_id, session
        )
        if existing_bytes + len(data) > _MAX_CONV_BYTES:
            return None
        final_name = _available_name(clean, existing_names)
        upload_path = f"uploads/{final_name}"
        await runtime.workspace.record_mutation_locked(
            conversation_id,
            "upload.write",
            paths=(_RESOURCE_MANIFEST_PATH, upload_path),
        )
        await session.write_file(upload_path, data)
        runtime.uploads.store(conversation_id, final_name, data)
        await _fold_upload_resource_ref(session, conversation_id, final_name, data)
        return final_name


def _index_upload(
    runtime: ConversationRuntime,
    conversation_id: str,
    final_name: str,
    data: bytes,
) -> None:
    upload_doc = parse_upload_to_doc(final_name, data, conversation_id)
    if upload_doc is not None and upload_doc.passages:
        runtime.deep_research.add_upload_passages(conversation_id, list(upload_doc.passages))


async def _announce_uploads(
    store: SqliteEventStore,
    conversation_id: str,
    saved: list[dict[str, Any]],
) -> None:
    parts = ", ".join(f"uploads/{item['name']} ({item['bytes']:,} bytes)" for item in saved)
    await store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=f"User uploaded: {parts}"),
        ),
    )
    if len(saved) == 1:
        name = saved[0]["name"]
        datasource_name = f"uploads/{name}"
        docs = f"path=uploads/{name} size={saved[0]['bytes']:,} bytes"
    else:
        datasource_name = f"uploads/{len(saved)}_files"
        docs = "\n".join(f"- uploads/{item['name']}  ({item['bytes']:,} bytes)" for item in saved)
    await store.append(conversation_id, DatasourceEvent(name=datasource_name, docs=docs))


async def ingest_uploads(
    files: list[UploadFile],
    *,
    conversation_id: str,
    store: SqliteEventStore,
    runtime: ConversationRuntime,
) -> UploadBatch:
    session = runtime.sessions.upload_session(conversation_id)
    saved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for upload in files:
        raw_name = upload.filename or ""
        clean = _sanitize_name(raw_name)
        if clean is None:
            rejected.append({"name": raw_name, "reason": "empty filename after sanitization"})
            continue
        data = await upload.read()
        if len(data) > _MAX_FILE_BYTES:
            rejected.append(
                {
                    "name": raw_name,
                    "reason": f"file exceeds 25 MB limit ({len(data):,} bytes)",
                }
            )
            continue
        final_name = await _store_upload(runtime, conversation_id, session, clean, data)
        if final_name is None:
            rejected.append(
                {
                    "name": raw_name,
                    "reason": "conversation upload quota (100 MB) would be exceeded",
                }
            )
            continue
        _index_upload(runtime, conversation_id, final_name, data)
        saved.append({"name": final_name, "bytes": len(data)})
    if saved:
        await _announce_uploads(store, conversation_id, saved)
    return UploadBatch(saved=saved, rejected=rejected)


async def _read_artifact_bytes(
    runtime: ConversationRuntime,
    conversation_id: str,
    norm: str,
) -> bytes | None:
    if is_runtime_secret_path(norm):
        return None
    session = runtime.live_sessions.live_session(conversation_id)
    if session is not None:
        with contextlib.suppress(Exception):
            return await session.read_file(norm)
    project_store = runtime.projects.current_project_store()
    if project_store is not None and project_store.status() == StorageStatus.OK:
        with contextlib.suppress(Exception):
            workspace = project_store.path_for(conversation_id).resolve()
            resolved = (workspace / norm).resolve()
            if resolved.is_relative_to(workspace) and resolved.is_file():
                return resolved.read_bytes()
    return None


def _artifact_identity(path: str, inline: bool) -> tuple[str, str]:
    norm = posixpath.normpath(path)
    if posixpath.isabs(norm) or norm.startswith(".."):
        raise ArtifactRejected(404)
    _, ext = posixpath.splitext(norm)
    media_type = _ARTIFACT_TYPES.get(ext.lower())
    if media_type is None or (inline and ext.lower() != ".html"):
        raise ArtifactRejected(404)
    return norm, media_type


async def _committed_artifact(
    runtime: ConversationRuntime,
    conversation_id: str,
    norm: str,
    events: list[Any],
) -> bytes:
    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise ArtifactRejected(503, {"reason": "workspace_unsealed"})
    try:
        committed = resolve_committed_workspace(events, project_store, conversation_id)
        with project_store.open_verified_version(
            conversation_id,
            committed.event.version_seq,
        ) as version:
            if norm not in {entry.path for entry in version.files}:
                raise ArtifactRejected(404)
            return version.read_bytes(norm, max_bytes=_MAX_ARTIFACT_BYTES)
    except (WorkspaceCommitUnavailable, StorageError) as exc:
        raise ArtifactRejected(503, {"reason": "workspace_unsealed"}) from exc


async def resolve_artifact(
    path: str,
    *,
    inline: bool,
    conversation_id: str,
    store: SqliteEventStore,
    runtime: ConversationRuntime,
) -> ArtifactPayload:
    norm, media_type = _artifact_identity(path, inline)
    if norm not in await _declared_artifacts(store, conversation_id):
        raise ArtifactRejected(404)
    events = await store.get_events(conversation_id)
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        data = await _committed_artifact(runtime, conversation_id, norm, events)
    else:
        data = await _read_artifact_bytes(runtime, conversation_id, norm)
    if data is None or len(data) > _MAX_ARTIFACT_BYTES:
        raise ArtifactRejected(404)
    return ArtifactPayload(data=data, media_type=media_type, basename=posixpath.basename(norm))

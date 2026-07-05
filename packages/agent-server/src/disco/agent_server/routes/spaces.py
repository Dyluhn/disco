"""Spaces routes: persistent named corpora for grounded research."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval import DefaultCorpusService, DiskVectorStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..runtime import ConversationRuntime
from ..space_store import JsonSpaceStore, SpaceDocument, SpaceRecord
from ..uploads_ingest import parse_upload_to_doc
from ._common import _MAX_FILE_BYTES, _sanitize_name

_ALLOWED_EXTENSIONS = frozenset({".pdf", ".txt", ".md", ".html", ".htm"})


class CreateSpaceBody(BaseModel):
    name: str
    description: str = ""


def make_spaces_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None  # noqa: ARG001
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/spaces")
    async def list_spaces() -> dict:
        space_store = _space_store(runtime)
        return {
            "spaces": [row.summary() for row in space_store.list_spaces()],
            "status": "ok",
        }

    @router.post("/api/spaces")
    async def create_space(body: CreateSpaceBody) -> dict:
        space_store = _space_store(runtime)
        try:
            record = space_store.create(name=body.name, description=body.description)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_space", "message": str(exc)},
            ) from exc
        return {"space": record.detail()}

    @router.get("/api/spaces/{space_id}")
    async def get_space(space_id: str) -> dict:
        record = _get_space_or_404(_space_store(runtime), space_id)
        return {"space": record.detail()}

    @router.delete("/api/spaces/{space_id}")
    async def delete_space(space_id: str) -> dict:
        space_store = _space_store(runtime)
        if space_store.get(space_id) is None:
            raise HTTPException(status_code=404, detail={"reason": "space_not_found"})
        deleted = space_store.delete(space_id)
        vector_store = _space_vector_store(runtime)
        await vector_store.delete_namespace(space_id)
        return {"deleted": deleted, "space_id": space_id}

    @router.post("/api/spaces/{space_id}/documents")
    async def upload_space_documents(
        space_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        space_store = _space_store(runtime)
        record = _get_space_or_404(space_store, space_id)
        corpus_service = _space_corpus_service(runtime)
        existing_names = {doc.name for doc in record.documents}
        saved: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for upload in files:
            raw_name = upload.filename or ""
            clean = _sanitize_name(raw_name)
            if clean is None:
                rejected.append({"name": raw_name, "reason": "empty filename after sanitization"})
                continue
            ext = Path(clean).suffix.lower()
            if ext not in _ALLOWED_EXTENSIONS:
                rejected.append({
                    "name": raw_name,
                    "reason": "unsupported type; upload pdf, txt, md, or html",
                })
                continue

            data = await upload.read()
            if len(data) > _MAX_FILE_BYTES:
                rejected.append({
                    "name": raw_name,
                    "reason": f"file exceeds 25 MB limit ({len(data):,} bytes)",
                })
                continue

            final_name = _unique_name(clean, existing_names)
            doc = parse_upload_to_doc(
                final_name,
                data,
                space_id,
                source_scheme="space",
            )
            if doc is None:
                rejected.append({"name": raw_name, "reason": "could not extract supported text"})
                continue

            try:
                if doc.passages:
                    await corpus_service.ingest(
                        space_id,
                        owner_id=DEFAULT_OWNER_ID,
                        docs=[doc],
                    )
            except Exception as exc:  # noqa: BLE001
                rejected.append({
                    "name": raw_name,
                    "reason": f"ingest failed: {type(exc).__name__}: {exc}",
                })
                continue

            document = SpaceDocument(
                document_id=f"doc_{Path(final_name).stem}_{len(record.documents) + len(saved) + 1}",
                name=final_name,
                media_type=upload.content_type or _media_type_for_ext(ext),
                byte_count=len(data),
                passage_count=len(doc.passages),
                created_at=_now_from_store(),
            )
            record = space_store.add_document(space_id, document)
            existing_names.add(final_name)
            saved.append(document.model_dump(mode="json"))

        return {"saved": saved, "rejected": rejected, "space": record.detail()}

    return router


def _space_store(runtime: ConversationRuntime | None) -> JsonSpaceStore:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    project_store = runtime.project_store()
    if project_store.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "project_storage_unavailable",
                "status": project_store.status().value,
            },
        )
    root = project_store.root
    if root is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "project_storage_unavailable", "status": "unset"},
        )
    return JsonSpaceStore(root)


def _space_vector_store(runtime: ConversationRuntime | None) -> DiskVectorStore:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    getter = getattr(runtime, "space_vector_store", None)
    if getter is None:
        raise HTTPException(status_code=503, detail={"reason": "spaces_unavailable"})
    return cast(Callable[[], DiskVectorStore], getter)()


def _space_corpus_service(runtime: ConversationRuntime | None) -> DefaultCorpusService:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    getter = getattr(runtime, "space_corpus_service", None)
    if getter is None:
        raise HTTPException(status_code=503, detail={"reason": "spaces_unavailable"})
    return cast(Callable[[], DefaultCorpusService], getter)()


def _get_space_or_404(space_store: JsonSpaceStore, space_id: str) -> SpaceRecord:
    try:
        record = space_store.get(space_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_space_id", "message": str(exc)},
        ) from exc
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "space_not_found"})
    return record


def _unique_name(clean: str, existing_names: set[str]) -> str:
    if clean not in existing_names:
        return clean
    stem = Path(clean).stem
    suffix = Path(clean).suffix
    counter = 2
    while True:
        candidate = f"{stem}-{counter}{suffix}"
        if candidate not in existing_names:
            return candidate
        counter += 1


def _media_type_for_ext(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(ext, "application/octet-stream")


def _now_from_store() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["make_spaces_router"]

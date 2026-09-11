"""Reference Packs routes: the user's persistent, reusable file collections.

Settings manages what exists (list, rename, describe, add/replace/remove
files, delete); creation is the Agent's ``create_reference_pack`` action. Every
route is owner-scoped; a pack that is not yours is indistinguishable from one
that does not exist (404).
"""

from __future__ import annotations

from typing import Annotated

from disco.tools.projects import StorageStatus
from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from ..auth import current_owner_id
from ..reference_pack_store import (
    JsonReferencePackStore,
    ReferencePackError,
    ReferencePackRecord,
    media_type_for,
)
from ..runtime import ConversationRuntime

_MAX_FILES_PER_REQUEST = 20


class UpdatePackBody(BaseModel):
    name: str | None = None
    description: str | None = None


def reference_pack_store(runtime: ConversationRuntime | None) -> JsonReferencePackStore:
    """The store under the current projects root (the same root Spaces use)."""
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    project_store = runtime.projects.current_project_store()
    root = project_store.root
    if project_store.status() != StorageStatus.OK or root is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "project_storage_unavailable",
                "status": project_store.status().value,
            },
        )
    return JsonReferencePackStore(root)


def _pack_or_404(record: ReferencePackRecord | None) -> ReferencePackRecord:
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "reference_pack_not_found"})
    return record


def _refused(exc: ReferencePackError) -> HTTPException:
    return HTTPException(
        status_code=400, detail={"reason": "invalid_reference_pack", "message": str(exc)}
    )


def make_reference_packs_router(runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/reference-packs")
    async def list_packs(request: Request) -> dict:
        store = reference_pack_store(runtime)
        return {"packs": [row.summary() for row in store.list(current_owner_id(request))]}

    @router.get("/api/reference-packs/{pack_id}")
    async def get_pack(pack_id: str, request: Request) -> dict:
        store = reference_pack_store(runtime)
        return {"pack": _pack_or_404(store.get(pack_id, current_owner_id(request))).detail()}

    @router.patch("/api/reference-packs/{pack_id}")
    async def update_pack(pack_id: str, body: UpdatePackBody, request: Request) -> dict:
        store = reference_pack_store(runtime)
        try:
            record = store.update_meta(
                pack_id, current_owner_id(request), name=body.name, description=body.description
            )
        except ReferencePackError as exc:
            raise _refused(exc) from exc
        return {"pack": _pack_or_404(record).detail()}

    @router.post("/api/reference-packs/{pack_id}/files")
    async def put_files(
        pack_id: str, request: Request, files: Annotated[list[UploadFile], File()]
    ) -> dict:
        """Add files, replacing any with the same name."""
        store = reference_pack_store(runtime)
        owner_id = current_owner_id(request)
        _pack_or_404(store.get(pack_id, owner_id))
        if len(files) > _MAX_FILES_PER_REQUEST:
            raise HTTPException(status_code=413, detail={"reason": "too_many_files_per_request"})
        record = None
        for upload in files:
            try:
                record = store.put_file(
                    pack_id, owner_id, upload.filename or "", await upload.read()
                )
            except ReferencePackError as exc:
                raise _refused(exc) from exc
        return {"pack": _pack_or_404(record).detail()}

    @router.delete("/api/reference-packs/{pack_id}/files/{name}")
    async def remove_file(pack_id: str, name: str, request: Request) -> dict:
        store = reference_pack_store(runtime)
        try:
            record = store.remove_file(pack_id, current_owner_id(request), name)
        except ReferencePackError as exc:
            raise _refused(exc) from exc
        return {"pack": _pack_or_404(record).detail()}

    @router.get("/api/reference-packs/{pack_id}/files/{name}")
    async def download_file(pack_id: str, name: str, request: Request) -> Response:
        store = reference_pack_store(runtime)
        data = store.read_file(pack_id, current_owner_id(request), name)
        if data is None:
            raise HTTPException(status_code=404, detail={"reason": "reference_pack_not_found"})
        media_type = media_type_for(name)
        inline = media_type.startswith(("text/", "image/")) or media_type == "application/pdf"
        disposition = "inline" if inline else "attachment"
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": f'{disposition}; filename="{name}"'},
        )

    @router.delete("/api/reference-packs/{pack_id}")
    async def delete_pack(pack_id: str, request: Request) -> dict:
        store = reference_pack_store(runtime)
        if not store.delete(pack_id, current_owner_id(request)):
            raise HTTPException(status_code=404, detail={"reason": "reference_pack_not_found"})
        return {"pack_id": pack_id, "deleted": True}

    return router

"""Optional Reference Pack CRUD/binding route factory.

The application composition root intentionally mounts this router separately.
The ``reader_provider`` is required for mutations so a request can never cause
the store to read the server process' current working directory.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from ..auth import current_owner_id
from ..reference_pack_binding import ReferencePackBindingConflict, ReferencePackBindingStore
from ..reference_pack_runtime import ReferencePackRuntime
from ..reference_pack_store import (
    ReferencePackError,
    ReferencePackForbidden,
    ReferencePackNotFound,
    ReferencePackStore,
)


class ReferencePackFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    name: str | None = None
    media_type: str | None = None


class ReferencePackCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    files: list[str | ReferencePackFileBody] = Field(min_length=1)
    conversation_id: str | None = None


class ReferencePackUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    files: list[str | ReferencePackFileBody] | None = None
    conversation_id: str | None = None


class ReferencePackBindBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[dict[str, str]] = Field(min_length=1)


def _raise(exc: Exception) -> None:
    if isinstance(exc, ReferencePackForbidden):
        raise HTTPException(status_code=403, detail={"reason": "reference_pack_forbidden"}) from exc
    if isinstance(exc, ReferencePackNotFound):
        raise HTTPException(status_code=404, detail={"reason": "reference_pack_not_found"}) from exc
    if isinstance(exc, ReferencePackBindingConflict):
        raise HTTPException(status_code=409, detail={"reason": str(exc)}) from exc
    if isinstance(exc, ReferencePackError):
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    raise exc


def make_reference_packs_router(
    store: ReferencePackStore | None = None,
    bindings: ReferencePackBindingStore | None = None,
    *,
    service: ReferencePackRuntime | None = None,
    reader_provider: Callable[[Request, str | None], Any] | None = None,
    conversation_authorizer: Callable[[Request, str], Any] | None = None,
) -> APIRouter:
    """Build routes; host wiring supplies the active sandbox reader provider."""

    if service is not None:
        if store is not None and store is not service.store:
            raise ValueError("Reference Pack router received two different library stores")
        if bindings is not None and bindings is not service.bindings:
            raise ValueError("Reference Pack router received two different binding stores")
        library = service.store
        snapshot_store = service.bindings
    else:
        library = store or ReferencePackStore()
        snapshot_store = bindings or ReferencePackBindingStore()
    router = APIRouter()

    async def authorize_conversation(request: Request, conversation_id: str | None) -> str:
        if not conversation_id:
            raise HTTPException(status_code=422, detail={"reason": "conversation_id_required"})
        if conversation_authorizer is None:
            raise HTTPException(
                status_code=409, detail={"reason": "conversation_authorizer_required"}
            )
        result = conversation_authorizer(request, conversation_id)
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise HTTPException(status_code=403, detail={"reason": "conversation_forbidden"})
        return conversation_id

    @router.get("/api/reference-packs")
    async def list_reference_packs(request: Request) -> dict[str, Any]:
        owner_id = current_owner_id(request)
        return {"packs": [pack.as_dict() for pack in library.list(owner_id)]}

    @router.get("/api/reference-packs/{pack_id}")
    async def get_reference_pack(pack_id: str, request: Request) -> dict[str, Any]:
        try:
            return library.get(current_owner_id(request), pack_id).as_dict()
        except Exception as exc:  # map only the narrow domain errors
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.post("/api/reference-packs")
    async def create_reference_pack(
        body: ReferencePackCreateBody, request: Request
    ) -> dict[str, Any]:
        if reader_provider is None:
            raise HTTPException(
                status_code=409, detail={"reason": "active_workspace_reader_required"}
            )
        try:
            conversation_id = await authorize_conversation(request, body.conversation_id)
            reader = reader_provider(request, conversation_id)
            if inspect.isawaitable(reader):
                reader = await reader
            pack = await library.acreate(
                current_owner_id(request),
                body.name,
                body.description,
                body.files,
                reader=reader,
            )
            return pack.as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.patch("/api/reference-packs/{pack_id}")
    async def update_reference_pack(
        pack_id: str, body: ReferencePackUpdateBody, request: Request
    ) -> dict[str, Any]:
        if body.files is not None and reader_provider is None:
            raise HTTPException(
                status_code=409, detail={"reason": "active_workspace_reader_required"}
            )
        try:
            reader = None
            if body.files is not None:
                conversation_id = await authorize_conversation(request, body.conversation_id)
                reader = reader_provider(request, conversation_id) if reader_provider else None
                if inspect.isawaitable(reader):
                    reader = await reader
            pack = await library.aupdate(
                current_owner_id(request),
                pack_id,
                name=body.name,
                description=body.description,
                files=body.files,
                reader=reader,
            )
            return pack.as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.put("/api/reference-packs/{pack_id}/files")
    async def update_reference_pack_files(pack_id: str, request: Request) -> dict[str, Any]:
        """Apply one authenticated add/replace/remove batch to a pack.

        Browser filenames are the pack-relative paths.  The store validates all
        paths and bytes before atomically swapping the mutable head, so a bad
        upload or removal cannot leave a half-applied library.
        """
        form = await request.form()
        uploads: list[tuple[str, bytes, str]] = []
        removals: list[str] = []
        uploaded_bytes = 0
        try:
            for key, value in form.multi_items():
                if key == "files":
                    if not isinstance(value, UploadFile):
                        raise HTTPException(
                            status_code=422, detail={"reason": "files must be uploads"}
                        )
                    data = await value.read(library.limits.max_file_bytes + 1)
                    if len(data) > library.limits.max_file_bytes:
                        raise ReferencePackError(
                            f"file exceeds the {library.limits.max_file_bytes}-byte limit: "
                            f"{value.filename or '<unnamed>'}"
                        )
                    uploaded_bytes += len(data)
                    if uploaded_bytes > library.limits.max_total_bytes:
                        raise ReferencePackError(
                            "uploaded files exceed the Reference Pack total-size limit"
                        )
                    uploads.append(
                        (
                            value.filename or "",
                            data,
                            value.content_type or "application/octet-stream",
                        )
                    )
                elif key == "remove":
                    if not isinstance(value, str):
                        raise HTTPException(
                            status_code=422, detail={"reason": "remove must be a file path"}
                        )
                    removals.append(value)
                else:
                    raise HTTPException(
                        status_code=422,
                        detail={"reason": f"unexpected multipart field: {key}"},
                    )
            if not uploads and not removals:
                raise HTTPException(
                    status_code=422, detail={"reason": "add files or remove a file"}
                )
            pack = library.update_files(
                current_owner_id(request), pack_id, uploads=uploads, remove=removals
            )
            return pack.as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc
        finally:
            await form.close()

    @router.delete("/api/reference-packs/{pack_id}")
    async def delete_reference_pack(pack_id: str, request: Request) -> dict[str, Any]:
        try:
            deleted = library.delete(current_owner_id(request), pack_id)
            return {"id": pack_id, "deleted": deleted}
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.post("/api/conversations/{conversation_id}/reference-packs/bind")
    async def bind_reference_packs(
        conversation_id: str, body: ReferencePackBindBody, request: Request
    ) -> dict[str, Any]:
        try:
            await authorize_conversation(request, conversation_id)
            binding = snapshot_store.bind(
                current_owner_id(request), conversation_id, body.selections, packs=library
            )
            return binding.as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.get("/api/conversations/{conversation_id}/reference-packs/bind")
    async def get_reference_binding(conversation_id: str, request: Request) -> dict[str, Any]:
        await authorize_conversation(request, conversation_id)
        binding = snapshot_store.get(current_owner_id(request), conversation_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"reason": "reference_binding_not_found"})
        return binding.as_dict()

    return router


__all__ = [
    "ReferencePackBindBody",
    "ReferencePackCreateBody",
    "ReferencePackFileBody",
    "ReferencePackUpdateBody",
    "make_reference_packs_router",
]

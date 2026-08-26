"""Route registration parts for the Reference Pack API."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile

from ..reference_pack_binding import ReferencePackBindingConflict
from ..reference_pack_store import (
    ReferencePackError,
    ReferencePackForbidden,
    ReferencePackNotFound,
)
from .reference_packs import (
    ReferencePackBindBody,
    ReferencePackCreateBody,
    ReferencePackUpdateBody,
)


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


async def _authorize(
    request: Request,
    conversation_id: str | None,
    authorizer: Callable[[Request, str], Any] | None,
) -> str:
    if not conversation_id:
        raise HTTPException(status_code=422, detail={"reason": "conversation_id_required"})
    if authorizer is None:
        raise HTTPException(status_code=409, detail={"reason": "conversation_authorizer_required"})
    result = authorizer(request, conversation_id)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise HTTPException(status_code=403, detail={"reason": "conversation_forbidden"})
    return conversation_id


def register_read_routes(
    router: APIRouter, library: Any, owner_getter: Callable[[Request], str]
) -> None:
    @router.get("/api/reference-packs")
    async def list_reference_packs(request: Request) -> dict[str, Any]:
        return {"packs": [pack.as_dict() for pack in library.list(owner_getter(request))]}

    @router.get("/api/reference-packs/{pack_id}")
    async def get_reference_pack(pack_id: str, request: Request) -> dict[str, Any]:
        try:
            return library.get(owner_getter(request), pack_id).as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc


def register_create_update_routes(
    router: APIRouter,
    library: Any,
    reader_provider: Callable[[Request, str | None], Any] | None,
    authorizer: Callable[[Request, str], Any] | None,
    owner_getter: Callable[[Request], str],
) -> None:
    @router.post("/api/reference-packs")
    async def create_reference_pack(
        body: ReferencePackCreateBody, request: Request
    ) -> dict[str, Any]:
        if reader_provider is None:
            raise HTTPException(
                status_code=409, detail={"reason": "active_workspace_reader_required"}
            )
        try:
            conversation_id = await _authorize(request, body.conversation_id, authorizer)
            reader = reader_provider(request, conversation_id)
            if inspect.isawaitable(reader):
                reader = await reader
            pack = await library.acreate(
                owner_getter(request), body.name, body.description, body.files, reader=reader
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
                conversation_id = await _authorize(request, body.conversation_id, authorizer)
                reader = reader_provider(request, conversation_id) if reader_provider else None
                if inspect.isawaitable(reader):
                    reader = await reader
            pack = await library.aupdate(
                owner_getter(request),
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


def register_upload_route(
    router: APIRouter, library: Any, owner_getter: Callable[[Request], str]
) -> None:
    @router.put("/api/reference-packs/{pack_id}/files")
    async def update_reference_pack_files(pack_id: str, request: Request) -> dict[str, Any]:
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
                            "file exceeds the "
                            f"{library.limits.max_file_bytes}-byte limit: "
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
                        status_code=422, detail={"reason": f"unexpected multipart field: {key}"}
                    )
            if not uploads and not removals:
                raise HTTPException(
                    status_code=422, detail={"reason": "add files or remove a file"}
                )
            return library.update_files(
                owner_getter(request), pack_id, uploads=uploads, remove=removals
            ).as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc
        finally:
            await form.close()


def register_delete_route(
    router: APIRouter, library: Any, owner_getter: Callable[[Request], str]
) -> None:
    @router.delete("/api/reference-packs/{pack_id}")
    async def delete_reference_pack(pack_id: str, request: Request) -> dict[str, Any]:
        try:
            deleted = library.delete(owner_getter(request), pack_id)
            return {"id": pack_id, "deleted": deleted}
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc


def register_binding_routes(
    router: APIRouter,
    library: Any,
    snapshot_store: Any,
    authorizer: Callable[[Request, str], Any] | None,
    owner_getter: Callable[[Request], str],
) -> None:
    @router.post("/api/conversations/{conversation_id}/reference-packs/bind")
    async def bind_reference_packs(
        conversation_id: str, body: ReferencePackBindBody, request: Request
    ) -> dict[str, Any]:
        try:
            await _authorize(request, conversation_id, authorizer)
            binding = snapshot_store.bind(
                owner_getter(request), conversation_id, body.selections, packs=library
            )
            return binding.as_dict()
        except Exception as exc:
            _raise(exc)
            raise AssertionError("unreachable") from exc

    @router.get("/api/conversations/{conversation_id}/reference-packs/bind")
    async def get_reference_binding(conversation_id: str, request: Request) -> dict[str, Any]:
        await _authorize(request, conversation_id, authorizer)
        binding = snapshot_store.get(owner_getter(request), conversation_id)
        if binding is None:
            raise HTTPException(status_code=404, detail={"reason": "reference_binding_not_found"})
        return binding.as_dict()

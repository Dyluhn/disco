"""Optional Reference Pack CRUD/binding route factory.

The application composition root intentionally mounts this router separately.
The ``reader_provider`` is required for mutations so a request can never cause
the store to read the server process' current working directory.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

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
        library, snapshot_store = service.store, service.bindings
    else:
        library = store or ReferencePackStore()
        snapshot_store = bindings or ReferencePackBindingStore()
    router = APIRouter()
    from ._reference_pack_route_parts import (
        register_binding_routes,
        register_create_update_routes,
        register_delete_route,
        register_read_routes,
        register_upload_route,
    )
    register_read_routes(router, library, current_owner_id)
    register_create_update_routes(
        router, library, reader_provider, conversation_authorizer, current_owner_id
    )
    register_upload_route(router, library, current_owner_id)
    register_delete_route(router, library, current_owner_id)
    register_binding_routes(
        router, library, snapshot_store, conversation_authorizer, current_owner_id
    )
    return router


__all__ = [
    "ReferencePackBindBody",
    "ReferencePackCreateBody",
    "ReferencePackFileBody",
    "ReferencePackUpdateBody",
    "make_reference_packs_router",
]

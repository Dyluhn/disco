"""Spaces routes: persistent folders for organizing conversations."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.base import ConversationSummary
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval import DiskVectorStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..runtime import ConversationRuntime
from ..space_store import JsonSpaceStore, SpaceRecord

_MEMBER_LIST_LIMIT = 100_000


class CreateSpaceBody(BaseModel):
    name: str
    description: str = ""


class UpdateSpaceBody(BaseModel):
    name: str | None = None
    description: str | None = None


def make_spaces_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/spaces")
    async def list_spaces() -> dict:
        space_store = _space_store(runtime)
        counts = await _member_counts(store)
        return {
            "spaces": [
                _with_member_count(row, counts.get(row.space_id, 0)).summary()
                for row in space_store.list_spaces()
            ],
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
        return {"space": record.detail() | {"members": []}}

    @router.get("/api/spaces/{space_id}")
    async def get_space(space_id: str) -> dict:
        record = _get_space_or_404(_space_store(runtime), space_id)
        members = await store.list_conversation_summaries(
            owner_id=DEFAULT_OWNER_ID,
            space_id=space_id,
            limit=_MEMBER_LIST_LIMIT,
        )
        space = _with_member_count(record, len(members)).detail()
        return {"space": space | {"members": [_conversation_summary(row) for row in members]}}

    @router.patch("/api/spaces/{space_id}")
    async def rename_space(space_id: str, body: UpdateSpaceBody) -> dict:
        space_store = _space_store(runtime)
        _get_space_or_404(space_store, space_id)
        try:
            record = space_store.rename(
                space_id,
                name=body.name,
                description=body.description,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_space", "message": str(exc)},
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"reason": "space_not_found"}) from exc
        members = await store.list_conversation_summaries(
            owner_id=DEFAULT_OWNER_ID,
            space_id=space_id,
            limit=_MEMBER_LIST_LIMIT,
        )
        space = _with_member_count(record, len(members)).detail()
        return {"space": space | {"members": [_conversation_summary(row) for row in members]}}

    @router.delete("/api/spaces/{space_id}")
    async def delete_space(space_id: str) -> dict:
        space_store = _space_store(runtime)
        if space_store.get(space_id) is None:
            raise HTTPException(status_code=404, detail={"reason": "space_not_found"})
        await store.clear_space_members(space_id)
        deleted = space_store.delete(space_id)
        vector_store = _space_vector_store(runtime)
        await vector_store.delete_namespace(space_id)
        return {"deleted": deleted, "space_id": space_id}

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


def _with_member_count(record: SpaceRecord, member_count: int) -> SpaceRecord:
    return record.model_copy(update={"member_count": member_count})


async def _member_counts(store: SqliteEventStore) -> dict[str, int]:
    summaries = await store.list_conversation_summaries(
        owner_id=DEFAULT_OWNER_ID,
        limit=_MEMBER_LIST_LIMIT,
    )
    counts: dict[str, int] = {}
    for summary in summaries:
        if summary.space_id:
            counts[summary.space_id] = counts.get(summary.space_id, 0) + 1
    return counts


def _conversation_summary(summary: ConversationSummary) -> dict:
    return {
        "id": summary.conversation_id,
        "owner_id": summary.owner_id,
        "space_id": summary.space_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "status": summary.status,
        "surface": summary.surface,
        "origin": summary.origin,
    }


__all__ = ["make_spaces_router"]

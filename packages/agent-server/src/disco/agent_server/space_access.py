"""Owner-scoped Space access helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from disco.tools.projects import StorageStatus
from fastapi import HTTPException

from .space_store import JsonSpaceStore

if TYPE_CHECKING:
    from .runtime import ConversationRuntime


def space_store_for_runtime(runtime: ConversationRuntime | None) -> JsonSpaceStore:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    project_store = runtime._projects.current_project_store()
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


def normalize_space_ids(space_ids: Iterable[object]) -> frozenset[str]:
    return frozenset(str(space_id).strip() for space_id in space_ids if str(space_id).strip())


def owned_space_ids_or_403(
    runtime: ConversationRuntime | None,
    space_ids: Iterable[object],
    *,
    owner_id: str,
    include_unclaimed_legacy: bool = False,
) -> frozenset[str]:
    requested = normalize_space_ids(space_ids)
    if not requested:
        return frozenset()
    space_store = space_store_for_runtime(runtime)
    forbidden: list[str] = []
    for space_id in sorted(requested):
        try:
            record = space_store.get(
                space_id,
                owner_id=owner_id,
                include_unclaimed_legacy=include_unclaimed_legacy,
            )
        except ValueError:
            record = None
        if record is None:
            forbidden.append(space_id)
    if forbidden:
        raise HTTPException(
            status_code=403,
            detail={"reason": "space_forbidden", "space_ids": forbidden},
        )
    return requested

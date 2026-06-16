"""Server-side directory picker route (settings projects_root selector)."""

from __future__ import annotations

from pathlib import Path

from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import validate_root
from fastapi import APIRouter, HTTPException, Query

from ..runtime import ConversationRuntime


def make_storage_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/storage/browse")
    async def browse_storage(path: str = Query(default="")) -> dict:
        """Server-side directory picker. Lists IMMEDIATE children of `path` (or
        $HOME when empty). Returns `{path, parent, entries: [{name, is_dir}]}`.
        Only directory listings; never returns file contents — the endpoint
        exists ONLY to drive the settings path picker."""
        try:
            target = Path(path).expanduser().resolve() if path else Path.home()
        except Exception as exc:  # noqa: BLE001 — bad path: 400 with the reason
            raise HTTPException(
                status_code=400, detail={"reason": "bad_path", "message": str(exc)}
            ) from exc
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail={"reason": "not_found", "path": str(target)},
            )
        if not target.is_dir():
            raise HTTPException(
                status_code=400,
                detail={"reason": "not_a_directory", "path": str(target)},
            )
        try:
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                # hide dotfiles — the picker is for project storage, not system browsing
                if child.name.startswith("."):
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                entries.append({"name": child.name, "is_dir": is_dir})
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"reason": "not_readable", "path": str(target)},
            ) from exc
        parent = str(target.parent) if target.parent != target else None
        # Whether the CURRENT path is selectable as a projects_root.
        select_status = validate_root(str(target)).value
        return {
            "path": str(target),
            "parent": parent,
            "entries": entries,
            "selectable": select_status,
        }

    return router

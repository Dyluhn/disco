"""Server-side directory picker route (settings projects_root selector)."""

from __future__ import annotations

from pathlib import Path

from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import validate_root
from fastapi import APIRouter, HTTPException, Query, Request

from ..auth import require_admin_session
from ..runtime import ConversationRuntime


def _is_forbidden_root(path: Path) -> bool:
    if path == Path("/"):
        return True
    return any(
        path == root or path.is_relative_to(root)
        for root in (Path("/etc"), Path("/root"))
    )


def _browse_allowed_roots(runtime: ConversationRuntime | None) -> list[Path]:
    candidates: list[Path] = [Path.home(), Path("/mnt"), Path("/media")]
    if runtime is not None:
        try:
            project_root = runtime.project_store().root
        except Exception:  # noqa: BLE001 - storage config may itself be invalid
            project_root = None
        if project_root is not None:
            candidates.append(project_root)

    roots: list[Path] = []
    for raw in candidates:
        try:
            root = raw.expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if _is_forbidden_root(root):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _ensure_allowed_browse_path(target: Path, runtime: ConversationRuntime | None) -> None:
    if _is_forbidden_root(target):
        raise HTTPException(
            status_code=403,
            detail={"reason": "outside_allowed_roots", "path": str(target)},
        )
    if any(
        target == root or target.is_relative_to(root)
        for root in _browse_allowed_roots(runtime)
    ):
        return
    raise HTTPException(
        status_code=403,
        detail={"reason": "outside_allowed_roots", "path": str(target)},
    )


def make_storage_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/storage/browse")
    async def browse_storage(request: Request, path: str = Query(default="")) -> dict:
        """Server-side directory picker. Lists IMMEDIATE children of `path` (or
        $HOME when empty). Returns `{path, parent, entries: [{name, is_dir}]}`.
        Only directory listings; never returns file contents — the endpoint
        exists ONLY to drive the settings path picker."""
        require_admin_session(request)
        try:
            target = Path(path).expanduser().resolve() if path else Path.home().resolve()
        except Exception as exc:  # noqa: BLE001 — bad path: 400 with the reason
            raise HTTPException(
                status_code=400, detail={"reason": "bad_path", "message": str(exc)}
            ) from exc
        if _is_forbidden_root(target):
            _ensure_allowed_browse_path(target, runtime)
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
        _ensure_allowed_browse_path(target, runtime)
        try:
            entries = []
            for child in sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            ):
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

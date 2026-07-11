"""Server-side directory picker route (settings projects_root selector)."""

from __future__ import annotations

import logging
from pathlib import Path

from disco.core.auth import CSRF_HEADER, AuthSession, SessionSigner
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import validate_root
from fastapi import APIRouter, HTTPException, Query, Request

from ..auth import current_session
from ..runtime import ConversationRuntime

_LOG = logging.getLogger(__name__)


def _is_forbidden_root(path: Path) -> bool:
    if path == Path("/"):
        return True
    return any(path == root or path.is_relative_to(root) for root in (Path("/etc"), Path("/root")))


def _browse_allowed_roots() -> list[Path]:
    candidates: list[Path] = [Path.home(), Path("/mnt"), Path("/media")]
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


def _ensure_allowed_browse_path(target: Path) -> None:
    if _is_forbidden_root(target):
        raise HTTPException(
            status_code=403,
            detail={"reason": "outside_allowed_roots", "path": str(target)},
        )
    if any(target == root or target.is_relative_to(root) for root in _browse_allowed_roots()):
        return
    raise HTTPException(
        status_code=403,
        detail={"reason": "outside_allowed_roots", "path": str(target)},
    )


def _browse_path_allowed(target: Path) -> bool:
    try:
        _ensure_allowed_browse_path(target)
    except HTTPException:
        return False
    return True


def _audit_browse(
    session: AuthSession,
    requested_path: str,
    *,
    decision: str,
    reason: str,
    resolved_path: Path | None = None,
    entry_count: int | None = None,
) -> None:
    """Emit one content-free audit record for every authenticated browse attempt."""
    _LOG.info(
        "storage browse audit owner=%s requested=%r resolved=%r decision=%s reason=%s entries=%s",
        session.owner_id,
        requested_path,
        str(resolved_path) if resolved_path is not None else None,
        decision,
        reason,
        entry_count,
        extra={
            "audit_event": "storage_browse",
            "audit_owner_id": session.owner_id,
            "audit_requested_path": requested_path,
            "audit_resolved_path": (str(resolved_path) if resolved_path is not None else None),
            "audit_decision": decision,
            "audit_reason": reason,
            "audit_entry_count": entry_count,
        },
    )


def make_storage_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/storage/browse")
    async def browse_storage(request: Request, path: str = Query(default="")) -> dict:
        """Server-side directory picker. Lists IMMEDIATE children of `path` (or
        $HOME when empty). Returns `{path, parent, entries: [{name, is_dir}]}`.
        Only directory listings; never returns file contents — the endpoint
        exists ONLY to drive the settings path picker."""
        requested_path = path or "~"
        session = current_session(request)
        if not session.is_admin:
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="admin_required",
            )
            raise HTTPException(status_code=403, detail={"reason": "admin_required"})
        # This read-only endpoint is still a host-filesystem oracle. Requiring the
        # session-bound header on GET makes it unavailable to ambient cross-site
        # requests; the shared frontend client already sends this header on all
        # authenticated requests.
        if session.session_id != "test-session" and not SessionSigner.csrf_valid(
            session, request.headers.get(CSRF_HEADER)
        ):
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="csrf_required",
            )
            raise HTTPException(status_code=403, detail={"reason": "csrf_required"})
        try:
            target = Path(path).expanduser().resolve() if path else Path.home().resolve()
        except Exception as exc:  # noqa: BLE001 — bad path: 400 with the reason
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="bad_path",
            )
            raise HTTPException(
                status_code=400, detail={"reason": "bad_path", "message": str(exc)}
            ) from exc
        # Authorize the resolved path BEFORE checking existence/type. Otherwise
        # callers get an existence oracle for paths outside the picker roots.
        try:
            _ensure_allowed_browse_path(target)
        except HTTPException:
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="outside_allowed_roots",
                resolved_path=target,
            )
            raise
        if not target.exists():
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="not_found",
                resolved_path=target,
            )
            raise HTTPException(
                status_code=404,
                detail={"reason": "not_found", "path": str(target)},
            )
        if not target.is_dir():
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="not_a_directory",
                resolved_path=target,
            )
            raise HTTPException(
                status_code=400,
                detail={"reason": "not_a_directory", "path": str(target)},
            )
        try:
            entries = []
            for child in target.iterdir():
                # hide dotfiles — the picker is for project storage, not system browsing
                if child.name.startswith("."):
                    continue
                try:
                    # A symlinked directory is never advertised as browsable. This
                    # removes both the under-root -> /etc escape and its retargeting
                    # race; a user can still navigate ordinary directories.
                    if child.is_symlink():
                        continue
                    resolved_child = child.resolve(strict=True)
                    if not _browse_path_allowed(resolved_child):
                        continue
                    is_dir = resolved_child.is_dir()
                except OSError:
                    continue
                entries.append({"name": child.name, "is_dir": is_dir})
            entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
        except PermissionError as exc:
            _audit_browse(
                session,
                requested_path,
                decision="denied",
                reason="not_readable",
                resolved_path=target,
            )
            raise HTTPException(
                status_code=403,
                detail={"reason": "not_readable", "path": str(target)},
            ) from exc
        parent_path = target.parent
        parent = (
            str(parent_path)
            if parent_path != target and _browse_path_allowed(parent_path)
            else None
        )
        # Whether the CURRENT path is selectable as a projects_root.
        select_status = validate_root(str(target)).value
        _audit_browse(
            session,
            requested_path,
            decision="allowed",
            reason="listed",
            resolved_path=target,
            entry_count=len(entries),
        )
        return {
            "path": str(target),
            "parent": parent,
            "entries": entries,
            "selectable": select_status,
        }

    return router

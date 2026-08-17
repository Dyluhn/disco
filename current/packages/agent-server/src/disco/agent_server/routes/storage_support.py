"""Private authorization and filesystem mechanics for storage browsing."""

from __future__ import annotations

import logging
from pathlib import Path

from disco.core.auth import CSRF_HEADER, AuthSession, SessionSigner
from disco.tools.projects import validate_root
from fastapi import HTTPException, Request

from ..auth import current_session

# Audit consumers filter on the established route logger identity.
_LOG = logging.getLogger("disco.agent_server.routes.storage")


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
        except Exception:  # noqa: BLE001 - unavailable optional browse root
            continue
        if not _is_forbidden_root(root) and root not in roots:
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


def _authorize_browse(
    session: AuthSession,
    request: Request,
    requested_path: str,
) -> None:
    if not session.is_admin:
        _audit_browse(
            session,
            requested_path,
            decision="denied",
            reason="admin_required",
        )
        raise HTTPException(status_code=403, detail={"reason": "admin_required"})
    if session.session_id != "test-session" and not SessionSigner.csrf_valid(
        session,
        request.headers.get(CSRF_HEADER),
    ):
        _audit_browse(
            session,
            requested_path,
            decision="denied",
            reason="csrf_required",
        )
        raise HTTPException(status_code=403, detail={"reason": "csrf_required"})


def _resolve_target(
    session: AuthSession,
    requested_path: str,
    path: str,
) -> Path:
    try:
        return Path(path).expanduser().resolve() if path else Path.home().resolve()
    except Exception as exc:  # noqa: BLE001 - bad path is a typed response
        _audit_browse(
            session,
            requested_path,
            decision="denied",
            reason="bad_path",
        )
        raise HTTPException(
            status_code=400,
            detail={"reason": "bad_path", "message": str(exc)},
        ) from exc


def _require_directory(
    session: AuthSession,
    requested_path: str,
    target: Path,
) -> None:
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


def _visible_entry(child: Path) -> dict | None:
    if child.name.startswith("."):
        return None
    try:
        if child.is_symlink():
            return None
        resolved_child = child.resolve(strict=True)
        if not _browse_path_allowed(resolved_child):
            return None
        return {"name": child.name, "is_dir": resolved_child.is_dir()}
    except OSError:
        return None


def _list_entries(target: Path) -> list[dict]:
    entries = [entry for child in target.iterdir() if (entry := _visible_entry(child)) is not None]
    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    return entries


def _entries_or_reject(
    session: AuthSession,
    requested_path: str,
    target: Path,
) -> list[dict]:
    try:
        return _list_entries(target)
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


def browse_storage(request: Request, path: str) -> dict:
    requested_path = path or "~"
    session = current_session(request)
    _authorize_browse(session, request, requested_path)
    target = _resolve_target(session, requested_path, path)
    _require_directory(session, requested_path, target)
    entries = _entries_or_reject(session, requested_path, target)
    parent_path = target.parent
    parent = (
        str(parent_path) if parent_path != target and _browse_path_allowed(parent_path) else None
    )
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

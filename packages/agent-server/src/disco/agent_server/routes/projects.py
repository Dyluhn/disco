"""Build-project routes — persistence list / download / manifest / delete."""

from __future__ import annotations

import asyncio
import contextlib
import io
import posixpath
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore, install_owner_id
from disco.tools.projects import (
    ProjectRecord,
    ProjectStore,
    StorageError,
    StorageStatus,
    VersionRecord,
    aiter_zip_workspace,
    is_runtime_secret_path,
)
from disco.tools.projects.store import tree_digest_of_files
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from ..auth import current_owner_id, current_session, require_admin_session
from ..runtime import ConversationRuntime
from ..title_service import fallback_title
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import require_owned_conversation

_MAX_IMPORT_ZIP_BYTES = 50 * 1024 * 1024
_MAX_IMPORT_TREE_BYTES = 200 * 1024 * 1024
_MAX_IMPORT_FILES = 2000
_GIT_CLONE_TIMEOUT_S = 120


async def _consumer_workspace(
    event_store: SqliteEventStore,
    project_store: ProjectStore,
    conversation_id: str,
) -> tuple[Path | None, VersionRecord | None, int | None, list]:
    """Resolve immutable bytes for FINISHED, mutable recovery bytes otherwise."""

    events = await event_store.get_events(conversation_id)
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        try:
            committed = resolve_committed_workspace(events, project_store, conversation_id)
        except WorkspaceCommitUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "workspace_unsealed"},
            ) from exc
        return None, committed.record, committed.event.seq, events
    return project_store.path_for(conversation_id), None, None, events


@dataclass(frozen=True)
class _ImportFile:
    src: Path
    rel: str
    size: int


@dataclass(frozen=True)
class _ImportStats:
    files: int
    bytes: int
    largest_path: str | None
    largest_bytes: int


@dataclass(frozen=True)
class _ImportSource:
    kind: str
    label: str
    title_seed: str
    root: Path | None = None
    zip_bytes: bytes | None = None
    skip_git_dir: bool = False


class _ImportRejected(ValueError):
    def __init__(self, status_code: int, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.status_code = status_code
        self.reason = reason
        self.message = message or reason


def _reject_import(status_code: int, reason: str, message: str | None = None) -> NoReturn:
    raise _ImportRejected(status_code, reason, message)


def _cap_check(files: int, total_bytes: int) -> None:
    if files > _MAX_IMPORT_FILES:
        _reject_import(
            413,
            "too_many_files",
            f"import has {files} files; max is {_MAX_IMPORT_FILES}",
        )
    if total_bytes > _MAX_IMPORT_TREE_BYTES:
        _reject_import(
            413,
            "tree_too_large",
            f"import has {total_bytes} bytes; max is {_MAX_IMPORT_TREE_BYTES}",
        )


def _safe_zip_rel(raw_name: str) -> str:
    raw = raw_name.replace("\\", "/")
    norm = posixpath.normpath(raw)
    if norm in {"", ".", ".."} or posixpath.isabs(norm) or norm.startswith("../"):
        _reject_import(400, "zip_slip", f"zip entry escapes the project root: {raw_name!r}")
    parts = PurePosixPath(norm).parts
    if any(part in {"", ".", ".."} for part in parts):
        _reject_import(400, "zip_slip", f"zip entry escapes the project root: {raw_name!r}")
    if is_runtime_secret_path(norm):
        _reject_import(
            400,
            "runtime_secret_file",
            f"import contains forbidden runtime secret path: {raw_name!r}",
        )
    return norm


def _repo_name_from_url(git_url: str) -> str:
    trimmed = git_url.rstrip("/").split("/")[-1] or "imported project"
    if trimmed.endswith(".git"):
        trimmed = trimmed[:-4]
    return trimmed or "imported project"


def _title_from_seed(seed: str) -> str:
    return fallback_title(seed) or "Imported project"


def _scan_import_tree(src: Path, *, skip_git_dir: bool) -> tuple[list[_ImportFile], _ImportStats]:
    root = src.resolve()
    files: list[_ImportFile] = []
    total_bytes = 0
    largest_path: str | None = None
    largest_bytes = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            _reject_import(
                400,
                "runtime_secret_file",
                f"import contains forbidden runtime secret path: {rel!r}",
            )
        if skip_git_dir and ".git" in PurePosixPath(rel).parts:
            continue
        size = path.stat().st_size
        files.append(_ImportFile(src=path, rel=rel, size=size))
        total_bytes += size
        if size > largest_bytes:
            largest_path = rel
            largest_bytes = size
        _cap_check(len(files), total_bytes)
    if not files:
        _reject_import(400, "no_files", "import source did not contain any files")
    return files, _ImportStats(
        files=len(files),
        bytes=total_bytes,
        largest_path=largest_path,
        largest_bytes=largest_bytes,
    )


def _copy_scanned_tree(files: list[_ImportFile], workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for item in files:
        dest = workspace / item.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.src, dest)


def _materialize_zip(zip_bytes: bytes, workspace: Path) -> _ImportStats:
    if len(zip_bytes) > _MAX_IMPORT_ZIP_BYTES:
        _reject_import(
            413,
            "zip_too_large",
            f"zip upload has {len(zip_bytes)} bytes; max is {_MAX_IMPORT_ZIP_BYTES}",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        _reject_import(400, "invalid_zip", str(exc))
    with zf:
        entries: list[tuple[zipfile.ZipInfo, str]] = []
        total_bytes = 0
        largest_path: str | None = None
        largest_bytes = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_zip_rel(info.filename)
            total_bytes += int(info.file_size)
            entries.append((info, rel))
            if info.file_size > largest_bytes:
                largest_path = rel
                largest_bytes = int(info.file_size)
            _cap_check(len(entries), total_bytes)
        if not entries:
            _reject_import(400, "no_files", "zip did not contain any files")

        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        actual_total = 0
        for info, rel in entries:
            data = zf.read(info)
            actual_total += len(data)
            _cap_check(len(entries), actual_total)
            dest = workspace / rel
            resolved = dest.resolve()
            root = workspace.resolve()
            if not resolved.is_relative_to(root):
                _reject_import(400, "zip_slip", f"zip entry escapes the project root: {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return _ImportStats(
            files=len(entries),
            bytes=actual_total,
            largest_path=largest_path,
            largest_bytes=largest_bytes,
        )


async def _clone_git_url(git_url: str, dest: Path) -> None:
    git = shutil.which("git")
    if git is None:
        _reject_import(400, "git_missing", "git is not installed on the agent-server host")
    assert git is not None
    try:
        proc = await asyncio.create_subprocess_exec(
            git,
            "clone",
            "--depth",
            "1",
            git_url,
            str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        _reject_import(400, "git_clone_failed", f"failed to launch git: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GIT_CLONE_TIMEOUT_S)
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise _ImportRejected(400, "git_clone_failed", "git clone timed out") from exc
    if proc.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip() or "git clone failed"
        _reject_import(400, "git_clone_failed", detail[:1000])


async def _parse_import_source(request: Request) -> _ImportSource:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        try:
            uploads = [value for _key, value in form.multi_items() if isinstance(value, UploadFile)]
            if len(uploads) != 1:
                _reject_import(
                    400, "invalid_request", "multipart import requires exactly one zip file"
                )
            upload = uploads[0]
            filename = upload.filename or "project.zip"
            if not filename.lower().endswith(".zip"):
                _reject_import(400, "invalid_zip", "uploaded project must be a .zip file")
            data = await upload.read()
            return _ImportSource(
                kind="zip",
                label=filename,
                title_seed=Path(filename).stem or filename,
                zip_bytes=data,
            )
        finally:
            await form.close()

    try:
        body: Any = await request.json()
    except Exception as exc:  # noqa: BLE001
        _reject_import(400, "invalid_request", f"request body must be JSON or multipart: {exc}")
    if not isinstance(body, dict):
        _reject_import(400, "invalid_request", "JSON body must be an object")
    path_value = body.get("path")
    git_url_value = body.get("git_url")
    supplied = [v for v in (path_value, git_url_value) if isinstance(v, str) and v.strip()]
    if len(supplied) != 1:
        _reject_import(400, "invalid_request", "supply exactly one of path or git_url")
    if isinstance(path_value, str) and path_value.strip():
        require_admin_session(request)
        root = Path(path_value).expanduser()
        if not root.exists():
            _reject_import(400, "path_not_found", f"path does not exist: {path_value}")
        if not root.is_dir():
            _reject_import(400, "path_not_directory", f"path is not a directory: {path_value}")
        return _ImportSource(
            kind="path",
            label=str(root),
            title_seed=root.name or str(root),
            root=root,
        )
    assert isinstance(git_url_value, str)
    git_url = git_url_value.strip()
    return _ImportSource(
        kind="git",
        label=git_url,
        title_seed=_repo_name_from_url(git_url),
        root=None,
        skip_git_dir=True,
    )


async def _created_at_for(
    store: SqliteEventStore, conversation_id: str, owner_id: str
) -> str | None:
    with contextlib.suppress(Exception):
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        row = next((s for s in summaries if s.conversation_id == conversation_id), None)
        if row is not None:
            return row.created_at
    return None


def _import_message(stats: _ImportStats, source_label: str) -> str:
    largest = (
        f"{stats.largest_path} ({stats.largest_bytes:,} bytes)"
        if stats.largest_path is not None
        else "n/a"
    )
    return (
        f"Imported {stats.files} files from {source_label}; "
        f"largest: {largest}. The workspace is already pre-populated."
    )


def _project_owner_for_session(
    raw_owner_id: str | None,
    session: AuthSession,
    *,
    legacy_unclaimed_owner: bool = False,
) -> str | None:
    owner_id = raw_owner_id.strip() if isinstance(raw_owner_id, str) else ""
    legacy_unclaimed = legacy_unclaimed_owner or not owner_id
    effective_owner = owner_id or install_owner_id()
    if effective_owner != session.owner_id:
        return None
    if legacy_unclaimed and not session.is_admin:
        return None
    return effective_owner


async def _handle_list_projects(
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict:
    session = current_session(request)
    owner_id = session.owner_id
    ps = runtime.project_store() if runtime is not None else None
    if ps is None:
        return {"projects": [], "status": StorageStatus.UNSET.value}
    status = ps.status()
    if status != StorageStatus.OK:
        return {"projects": [], "status": status.value, "root": str(ps.root or "")}
    records = ps.list_projects()
    summaries = await store.list_conversation_summaries(owner_id=owner_id, limit=500, cursor=None)
    by_id = {s.conversation_id: s for s in summaries}
    projects = []
    for r in records:
        record_owner = _project_owner_for_session(
            r.owner_id,
            session,
            legacy_unclaimed_owner=r.legacy_unclaimed_owner,
        )
        if record_owner is None:
            continue
        s = by_id.get(r.conversation_id)
        projects.append(
            {
                "id": r.conversation_id,
                "owner_id": record_owner,
                "title": (s.title if s else None) or r.title or "(untitled)",
                "surface": (s.surface if s else None) or "build",
                "created_at": (s.created_at if s else None) or r.created_at,
                "last_snapshot_at": r.last_snapshot_at,
                "file_count": r.file_count,
                "total_bytes": r.total_bytes,
                "files_missing": r.files_missing,
            }
        )
    return {"projects": projects, "status": status.value, "root": str(ps.root or "")}


async def _handle_import_project(
    request: Request,
    *,
    owner_id: str,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict:
    ps = runtime.project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=503,
            detail={"reason": "storage_unavailable"},
        )
    assert runtime is not None

    try:
        source = await _parse_import_source(request)
    except _ImportRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc

    title = _title_from_seed(source.title_seed)
    conversation_id = f"conv_{uuid.uuid4().hex}"
    workspace = ps.path_for(conversation_id)

    try:
        if source.kind == "zip":
            assert source.zip_bytes is not None
            stats = _materialize_zip(source.zip_bytes, workspace)
        elif source.kind == "git":
            with tempfile.TemporaryDirectory(prefix="disco-import-") as tmp:
                clone_root = Path(tmp) / "repo"
                await _clone_git_url(source.label, clone_root)
                files, stats = _scan_import_tree(clone_root, skip_git_dir=True)
                _copy_scanned_tree(files, workspace)
        else:
            assert source.root is not None
            files, stats = _scan_import_tree(source.root, skip_git_dir=source.skip_git_dir)
            _copy_scanned_tree(files, workspace)
    except _ImportRejected as exc:
        if workspace.exists():
            shutil.rmtree(workspace)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc
    except Exception as exc:
        if workspace.exists():
            shutil.rmtree(workspace)
        raise HTTPException(
            status_code=500,
            detail={"reason": "import_failed", "message": str(exc)},
        ) from exc

    store.create_conversation(
        conversation_id,
        owner_id=owner_id,
        title=title,
        surface="build",
    )
    runtime._settings._set_surface(conversation_id, "build")
    created_at = await _created_at_for(store, conversation_id, owner_id)
    ps.write_manifest(
        conversation_id,
        title=title,
        owner_id=owner_id,
        created_at=created_at,
        file_count=stats.files,
        total_bytes=stats.bytes,
        # Record import provenance durably: an imported project whose stack the
        # detector can't recognize must assess `needs_review`, not `not_web` (WO-3
        # rung 4). `write_manifest` preserves this across later re-snapshots.
        imported=True,
    )
    with contextlib.suppress(Exception):
        ps.cut_version(conversation_id, trigger="import")
    await store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=_import_message(stats, source.label),
            ),
        ),
    )
    return {
        "conversation_id": conversation_id,
        "files": stats.files,
        "bytes": stats.bytes,
        "title": title,
    }


async def _handle_backfill_titles(
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    retitle_fallbacks: bool,
) -> dict:
    """Maintenance: title any conversations still showing ``(untitled)``."""
    owner_id = current_owner_id(request)
    if runtime is None:
        return {"titled": {}, "scanned": 0, "status": "no-runtime"}
    summaries = await store.list_conversation_summaries(owner_id=owner_id, limit=500, cursor=None)
    candidates = [s.conversation_id for s in summaries if not s.title or retitle_fallbacks]
    titled = await runtime._title_service.backfill(
        candidates,
        retitle_fallbacks=retitle_fallbacks,
    )
    return {"titled": titled, "count": len(titled), "scanned": len(candidates)}


async def _handle_download_project(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> Response:
    """Stream a zip of the project's workspace."""
    ps = runtime.project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=404,
            detail={"reason": "storage_unavailable"},
        )
    record = ps.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
    if (
        _project_owner_for_session(
            record.owner_id,
            current_session(request),
            legacy_unclaimed_owner=record.legacy_unclaimed_owner,
        )
        is None
    ):
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})
    workspace, committed_record, _seal_seq, _events = await _consumer_workspace(
        store,
        ps,
        conversation_id,
    )
    # A FINISHED project is served from its immutable version. Loss of the
    # mutable recovery mirror must not hide still-verified committed bytes.
    if committed_record is None and record.files_missing:
        raise HTTPException(status_code=404, detail={"reason": "files_missing"})
    headers = {
        "Content-Disposition": (f'attachment; filename="{conversation_id}.zip"'),
    }
    # WO-4 (export): the source-bound self-host overlay — best-effort, computed
    # against the LIVE mirror and empty unless the tree matches a committed
    # candidate, so a diverged/unsnapshotted tree never gets a false self-host
    # bundle and a failed assessment never breaks the download.
    overlay_files: dict[str, str] = {}
    assessed_seq: int | None = None
    live_mirror = workspace if workspace is not None else ps.path_for(conversation_id)
    try:
        from .release import assess_project

        assessed = await asyncio.to_thread(
            assess_project, ps, record, live_mirror, record.conversation_id
        )
        overlay_files = assessed.overlay_files
        assessed_seq = assessed.response.version_seq
    except Exception:  # assessment must never break the download
        overlay_files = {}
        assessed_seq = None
    if committed_record is not None:
        try:
            with ps.open_verified_version(
                conversation_id,
                committed_record.seq,
            ) as verified:
                if overlay_files and assessed_seq == committed_record.seq:
                    # Merged contract: reliability's hash-verified committed bytes
                    # PLUS export's overlay, in one deterministic zip — the same
                    # combination rule as the bound download (a source file always
                    # wins a collision; a runtime-secret path is never emitted).
                    # The overlay attaches ONLY when the assessment matched this
                    # exact committed version, so the overlay always describes the
                    # bytes actually served.
                    combined = {
                        entry.path: verified.read_bytes(entry.path)
                        for entry in verified.files
                        if not is_runtime_secret_path(entry.path)
                    }
                    for rel, text in overlay_files.items():
                        if rel not in combined and not is_runtime_secret_path(rel):
                            combined[rel] = text.encode("utf-8")
                    payload = _deterministic_zip(combined)
                else:
                    payload = verified.zip_bytes()
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "workspace_unsealed"},
            ) from exc
        return Response(content=payload, media_type="application/zip", headers=headers)
    assert workspace is not None
    body = (
        _aiter_zip_with_overlay(workspace, overlay_files)
        if overlay_files
        else aiter_zip_workspace(workspace)
    )
    return StreamingResponse(body, media_type="application/zip", headers=headers)


async def _handle_project_manifest(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict:
    """Export a JSON manifest of the project: metadata, file tree, and deliverable."""
    ps = runtime.project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
    record = ps.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
    if (
        _project_owner_for_session(
            record.owner_id,
            current_session(request),
            legacy_unclaimed_owner=record.legacy_unclaimed_owner,
        )
        is None
    ):
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})
    # file tree (workspace-relative path + size), skipping the codeact scratch files
    files: list[dict] = []
    workspace, committed_record, seal_seq, events = await _consumer_workspace(
        store,
        ps,
        conversation_id,
    )
    if committed_record is not None:
        try:
            with ps.open_verified_version(
                conversation_id,
                committed_record.seq,
            ) as verified:
                files = [
                    {"path": entry.path, "bytes": entry.size}
                    for entry in verified.files
                    if not PurePosixPath(entry.path).name.startswith("_codeact")
                ]
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "workspace_unsealed"},
            ) from exc
    elif workspace and workspace.is_dir():
        for p in sorted(workspace.rglob("*")):
            rel = p.relative_to(workspace).as_posix()
            if (
                p.is_file()
                and not p.is_symlink()
                and not p.name.startswith("_codeact")
                and not is_runtime_secret_path(rel)
            ):
                files.append({"path": str(p.relative_to(workspace)), "bytes": p.stat().st_size})
    # the agent's last deliverable handoff, if any
    deliverable = None
    with contextlib.suppress(Exception):
        eligible_events = [
            event
            for event in events
            if seal_seq is None or event.seq is None or event.seq <= seal_seq
        ]
        for e in reversed(eligible_events):
            if isinstance(e, DeliverableEvent):
                deliverable = {
                    "title": e.title,
                    "path": e.path,
                    "kind": e.artifact_kind,
                    "deployment_url": e.deployment_url,
                }
                break
    return {
        "conversation_id": conversation_id,
        "title": record.title or "(untitled)",
        "created_at": record.created_at,
        "last_snapshot_at": record.last_snapshot_at,
        "file_count": (
            committed_record.file_count if committed_record is not None else record.file_count
        ),
        "total_bytes": (
            committed_record.total_bytes if committed_record is not None else record.total_bytes
        ),
        "files": files,
        "deliverable": deliverable,
    }


async def _handle_delete_project(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict:
    """Remove a project's manifest + workspace from disk."""
    ps = runtime.project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=404,
            detail={"reason": "storage_unavailable"},
        )
    record = ps.get(conversation_id)
    if record is None:
        return {"id": conversation_id, "deleted": False}
    if (
        _project_owner_for_session(
            record.owner_id,
            current_session(request),
            legacy_unclaimed_owner=record.legacy_unclaimed_owner,
        )
        is None
    ):
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})
    if runtime is None:
        deleted = ps.delete(conversation_id)
    else:
        async with runtime._workspace.mutation(
            conversation_id,
            "project.delete",
            paths=(".",),
        ):
            deleted = ps.delete(conversation_id)
    return {"id": conversation_id, "deleted": deleted}


async def _resolve_project_for_read(
    request: Request,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
) -> tuple[ProjectStore, ProjectRecord, Path]:
    """The shared owner-scoped read preamble for the download + release endpoints.

    Mirrors the download endpoint's original checks in the SAME order, so both
    endpoints report identical auth/error semantics: 404 ``storage_unavailable``
    (storage unconfigured / invalid), 404 ``project_not_found`` (no manifest), 403
    ``project_forbidden`` (owned by another session), 404 ``files_missing`` (the
    workspace tree is gone). Returns the store, the record, and the workspace dir
    on success."""
    conversation_id = await require_owned_conversation(request, store, conversation_id)
    ps = runtime.project_store() if runtime is not None else None
    if ps is None or ps.status() != StorageStatus.OK:
        raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
    record = ps.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
    if (
        _project_owner_for_session(
            record.owner_id,
            current_session(request),
            legacy_unclaimed_owner=record.legacy_unclaimed_owner,
        )
        is None
    ):
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})
    if record.files_missing:
        raise HTTPException(status_code=404, detail={"reason": "files_missing"})
    workspace = ps.path_for(conversation_id)
    return ps, record, workspace


def _ancestor_dirs(rel: str) -> list[str]:
    """The directory prefixes of a normalized POSIX archive name, outermost first —
    e.g. ``a/b/c`` -> ``["a", "a/b"]``; a top-level name has none. Used to reserve the
    directories a written entry occupies so a later entry can never clash file-vs-dir."""
    parts = rel.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def _zip_workspace_with_overlay(src: Path, overlay: dict[str, str]) -> Iterator[bytes]:
    """Stream a zip of the workspace tree PLUS the generated self-host overlay.

    The workspace portion is emitted identically to ``zip_workspace`` (same sorted
    order, same ZIP_DEFLATED, same runtime-secret exclusion), so a candidate
    download's SOURCE bytes are unchanged from the plain zip and only the overlay
    files are added. A workspace file always wins a path collision, and a
    runtime-secret path is never emitted from the overlay — defense in depth over the
    caller's already-filtered map.

    The writer is HARDENED against unsafe overlay archive names (plan §10.7): an
    overlay entry is dropped when its normalized name would ESCAPE the extraction
    root (a `../` traversal / absolute path) or COLLIDE with an already-written name —
    exactly, case-insensitively (a Windows/macOS case-fold clash), or after slash
    normalization (`dir//f` onto `dir/f`). It ALSO reserves the DIRECTORY PREFIXES of
    every written name, so a generated file is never written where a workspace entry
    already occupies that path as a directory (a `compose.yaml/inner.txt` workspace
    file blocks a generated `compose.yaml` FILE) and vice-versa (a workspace FILE at an
    ancestor blocks a generated path beneath it). So the produced archive never carries
    a colliding, file-vs-directory, or escaping entry even for an adversarial overlay
    map — belt-and-suspenders behind the assess-time collision gate."""
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"workspace directory not found: {src}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        taken_norm: set[str] = set()
        taken_lower: set[str] = set()
        taken_dirs_norm: set[str] = set()
        taken_dirs_lower: set[str] = set()

        def _reserve(rel: str) -> None:
            taken_norm.add(rel)
            taken_lower.add(rel.lower())
            for parent in _ancestor_dirs(rel):
                taken_dirs_norm.add(parent)
                taken_dirs_lower.add(parent.lower())

        for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(src).as_posix()
            if is_runtime_secret_path(rel):
                continue
            zf.write(path, arcname=rel)
            _reserve(posixpath.normpath(rel))
        for raw in sorted(overlay):
            rel = posixpath.normpath(raw.replace("\\", "/"))
            if rel in {"", "."} or rel == ".." or rel.startswith("../") or posixpath.isabs(rel):
                continue  # a traversal / absolute name would escape the extraction root
            if is_runtime_secret_path(rel):
                continue
            if rel in taken_norm or rel.lower() in taken_lower:
                continue  # the workspace (or an earlier overlay entry) wins the collision
            if rel in taken_dirs_norm or rel.lower() in taken_dirs_lower:
                continue  # a workspace/overlay entry already occupies this path as a DIR
            ancestors = _ancestor_dirs(rel)
            if any(a in taken_norm or a.lower() in taken_lower for a in ancestors):
                continue  # an ANCESTOR of this path is already a FILE (a file/dir clash)
            zf.writestr(rel, overlay[raw].encode("utf-8"))
            _reserve(rel)
    buf.seek(0)
    chunk = 64 * 1024
    while True:
        block = buf.read(chunk)
        if not block:
            break
        yield block


async def _aiter_zip_with_overlay(src: Path, overlay: dict[str, str]) -> AsyncIterator[bytes]:
    """Async wrapper over ``_zip_workspace_with_overlay`` for StreamingResponse."""
    for block in _zip_workspace_with_overlay(src, overlay):
        yield block


# ---- bound (source-locked) self-host download (WO-C2) -------------------------
#
# The self-host action downloads with an IMMUTABLE binding
# `/download?version_seq=N&spec_digest=D`. BOTH values are enforced server-side: the
# zip is built from the exact committed version N's IMMUTABLE stored workspace
# (never the mutable live mirror), re-hash-verified against its `VersionRecord`
# digest, and its assessed `spec_digest` must equal D. A mismatch emits NO bytes
# and never silently falls back to an unbound/plain zip.

# The zip epoch (DOS zero-date) — a FIXED per-entry timestamp so two bound
# downloads of the same immutable version are byte-identical (the version bytes do
# not change, and no wall-clock time leaks into the archive metadata).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class _BoundReject(Exception):
    """A bound download that fails its binding — surfaced as a documented status
    (403 handled by the read preamble; 409/410 here) that emits no zip bytes."""

    def __init__(self, status_code: int, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.status_code = status_code
        self.reason = reason
        self.message = message or reason


def _deterministic_zip(files: dict[str, bytes]) -> bytes:
    """A fully deterministic zip of ``{rel: bytes}`` — sorted entry order, fixed
    per-entry timestamp, fixed mode, DEFLATE — so identical inputs produce
    byte-identical output. Built entirely in-memory (bounded by the snapshot caps)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(filename=rel, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[rel])
    return buf.getvalue()


def _version_source_files(root: Path) -> dict[str, bytes]:
    """The immutable file view of a committed version's stored workspace — the exact
    set ``store.tree_digest`` hashes (symlinks + runtime-secret paths excluded)."""
    files: dict[str, bytes] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            continue
        files[rel] = path.read_bytes()
    return files


def _build_bound_download_zip(
    ps: ProjectStore,
    record: ProjectRecord,
    conversation_id: str,
    version_seq: int,
    spec_digest_value: str,
) -> bytes:
    """Build the source-locked self-host zip for one committed version, enforcing the
    `version_seq` + `spec_digest` binding. Runs entirely off the event loop (called
    via ``asyncio.to_thread``). Raises ``_BoundReject`` (409/410, no bytes) when the
    version does not exist here, its stored bytes fail integrity, it is not a
    self-host candidate, or its assessed `spec_digest` does not equal the bound
    value. The zip source is version N's IMMUTABLE stored workspace — never the
    mutable live mirror — so a concurrent live edit / N+1 cut can never leak a mixed
    tree into a completed download.

    The version bytes are read EXACTLY ONCE into memory; the integrity digest, the
    release assessment, and the emitted zip all derive from that single snapshot. A
    concurrent `_prune` that deletes an UNPINNED version N's directory can therefore
    never tear the download across a read-vs-read window: the read either observes the
    whole tree (a wholly-N zip) or it fails atomically. A vanished/partial dir yields
    an empty-or-short snapshot whose digest cannot match the recorded one (typed 409),
    and a file that disappears mid-read raises `OSError` (typed 410) — never an untyped
    500 and never a torn/empty 200."""
    from .release import assess_release

    try:
        version_ws = ps.version_workspace_path(conversation_id, version_seq)
    except StorageError as exc:
        # Unknown sequence, another conversation's sequence, or a missing workspace:
        # the bound version is not available here.
        raise _BoundReject(410, "version_not_found", str(exc)) from exc
    version_record: VersionRecord | None = next(
        (v for v in ps.list_versions(conversation_id) if v.seq == version_seq), None
    )
    if version_record is None:
        raise _BoundReject(410, "version_not_found", f"no committed version {version_seq}")

    # ONE atomic read of the immutable committed source. A concurrent prune that
    # removes the whole dir makes `rglob` yield nothing (an empty snapshot, caught by
    # the digest guard below); a file that vanishes mid-read raises `OSError` here.
    try:
        source_files = _version_source_files(version_ws)
    except OSError as exc:
        raise _BoundReject(410, "version_not_found", "version workspace was removed") from exc

    # Integrity guard over the IN-MEMORY bytes (never a re-traversal): hashing the
    # snapshot reproduces `store.tree_digest(version_ws)` exactly for an intact tree,
    # so a pruned/torn read yields a digest that cannot equal the recorded one and
    # fails closed as a typed 409 — never a silent empty-200.
    if tree_digest_of_files(source_files) != version_record.tree_digest:
        raise _BoundReject(
            409, "source_integrity_failed", "version bytes no longer match the recorded digest"
        )

    # Assess the SAME in-memory snapshot (no second read of the version dir). A
    # corrupt/unreadable host-owned intent sidecar makes the read raise StorageError;
    # surface it as a TYPED bound-download reject that emits no bytes — NEVER a
    # broad-`except` fallback to a successful plain zip (plan §10.9).
    try:
        intent = ps.read_release_intent(conversation_id)
    except StorageError as exc:
        raise _BoundReject(500, "release_intent_unreadable", str(exc)) from exc
    assessed = assess_release(
        source_files,
        intent=intent,
        project_name=record.title or conversation_id,
        version_seq=version_record.seq,
        tree_digest=version_record.tree_digest,
        source_snapshotted=True,
        imported=record.imported,
    )
    response = assessed.response
    if not response.self_host or response.spec_digest is None:
        raise _BoundReject(
            409, "not_self_hostable", "the bound version is not a self-host candidate"
        )
    if response.spec_digest != spec_digest_value:
        raise _BoundReject(
            409, "spec_digest_mismatch", "spec_digest does not match the bound version"
        )

    combined = dict(source_files)
    for rel, text in assessed.overlay_files.items():
        # A workspace file always wins a collision (the overlay entry was already
        # dropped upstream); never emit a runtime-secret path.
        if rel not in combined and not is_runtime_secret_path(rel):
            combined[rel] = text.encode("utf-8")
    return _deterministic_zip(combined)


async def _aiter_bytes(payload: bytes) -> AsyncIterator[bytes]:
    chunk = 64 * 1024
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


async def _download_response(
    ps: ProjectStore,
    record: ProjectRecord,
    workspace: Path,
    version_seq: int | None,
    spec_digest: str | None,
) -> StreamingResponse:
    """Build the download response for an already owner-scoped project.

    DEFAULT (no binding query): the filtered workspace zip, plus the WO-4 self-host
    overlay when the project is a COMMITTED candidate (best-effort — a failed
    assessment falls back to the plain zip, never breaking the download).

    BOUND self-host (``version_seq`` + ``spec_digest``, WO-C2): the immutable
    committed version, hash-verified, with BOTH values enforced server-side; a
    mismatch emits NO bytes (409/410) and never falls back to a plain zip."""
    headers = {"Content-Disposition": f'attachment; filename="{record.conversation_id}.zip"'}
    if version_seq is not None or spec_digest is not None:
        if version_seq is None or spec_digest is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "incomplete_binding",
                    "message": "a bound download requires BOTH version_seq and spec_digest",
                },
            )
        try:
            # Integrity-verify + zip-build off the event loop; a bound download
            # streams the IMMUTABLE version workspace, never the mutable mirror.
            payload = await asyncio.to_thread(
                _build_bound_download_zip,
                ps,
                record,
                record.conversation_id,
                version_seq,
                spec_digest,
            )
        except _BoundReject as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"reason": exc.reason, "message": exc.message},
            ) from exc
        return StreamingResponse(
            _aiter_bytes(payload), media_type="application/zip", headers=headers
        )

    overlay_files: dict[str, str] = {}
    try:
        from .release import assess_project

        # The overlay comes from the source-bound assessment (the committed version
        # matching the live tree) — empty unless it is a candidate, so a diverged /
        # unsnapshotted tree never gets a false self-host bundle.
        overlay_files = (
            await asyncio.to_thread(assess_project, ps, record, workspace, record.conversation_id)
        ).overlay_files
    except Exception:  # assessment must never break the plain download
        overlay_files = {}
    body = (
        _aiter_zip_with_overlay(workspace, overlay_files)
        if overlay_files
        else aiter_zip_workspace(workspace)
    )
    return StreamingResponse(body, media_type="application/zip", headers=headers)


def make_projects_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    async def list_projects(
        request: Request,
    ) -> dict:
        """List Build projects with metadata, or a clear empty storage status."""
        return await _handle_list_projects(request, store=store, runtime=runtime)

    @router.post("/api/projects/backfill-titles")
    async def backfill_titles(
        request: Request,
        retitle_fallbacks: bool = Query(default=False),
    ) -> dict:
        """Maintenance: title any conversations still showing ``(untitled)`` — those
        created before auto-titling existed, or where the live title-gen failed.
        Re-runnable + idempotent (already-titled conversations are skipped). Uses the
        same SUMMARIZER-role model as the live auto-titler, with the first-message
        fallback. Returns ``{cid: title}`` for the ones it named.

        ``retitle_fallbacks=true`` additionally upgrades stored fallback-clamp
        titles (a question cut off mid-sentence) to real summarized titles — the
        repair pass for PDF covers minted before the summarizer retry existed."""
        return await _handle_backfill_titles(
            request,
            store=store,
            runtime=runtime,
            retitle_fallbacks=retitle_fallbacks,
        )

    @router.post("/api/projects/import")
    async def import_project(
        request: Request,
    ) -> dict:
        """Create a new Build conversation whose ProjectStore workspace is seeded
        from a zip upload, local directory, or shallow git clone."""
        owner_id = current_owner_id(request)
        return await _handle_import_project(
            request, owner_id=owner_id, store=store, runtime=runtime
        )

    @router.get("/api/projects/{conversation_id}/download")
    async def download_project(
        conversation_id: str,
        request: Request,
        version_seq: int | None = Query(default=None),
        spec_digest: str | None = Query(default=None),
    ) -> Response:
        """Stream a zip of the project's workspace (owner-scoped; 404 with a
        specific reason when the storage is unconfigured / the project is unknown /
        the files have been deleted under the manifest, 403 ``project_forbidden``).
        A DEFAULT download follows reliability's consumer-workspace projection — a
        FINISHED project serves its immutable, hash-verified committed version and
        loss of the mutable recovery mirror never hides still-verified committed
        bytes; a live project streams the filtered workspace zip — plus the WO-4
        self-host overlay for a committed candidate. A BOUND
        ``?version_seq=N&spec_digest=D`` download is the immutable, hash-verified
        version N with both values enforced server-side (WO-C2); an incomplete
        binding is a 409 and a failed binding emits no bytes."""
        if version_seq is not None or spec_digest is not None:
            ps, record, workspace = await _resolve_project_for_read(
                request, store, runtime, conversation_id
            )
            return await _download_response(ps, record, workspace, version_seq, spec_digest)
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_download_project(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    @router.get("/api/projects/{conversation_id}/manifest")
    async def project_manifest(conversation_id: str, request: Request) -> dict:
        """Export a JSON manifest of the project: metadata, the file tree (path +
        bytes), and the agent's last deliverable handoff (title/path/kind +
        deployment_url). The honest, portable description of what the run produced —
        the companion to the workspace zip download."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_project_manifest(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    @router.delete("/api/projects/{conversation_id}")
    async def delete_project(conversation_id: str, request: Request) -> dict:
        """Remove a project's manifest + workspace from disk. The conversation
        events in SQLite are left alone (deleting those is a separate concern,
        and matches the History surface's existing delete semantics)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_delete_project(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    return router

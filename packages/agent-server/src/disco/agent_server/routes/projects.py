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

from disco.core import DeliverableEvent, EventSource, LLMMessage, MessageEvent
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore, install_owner_id
from disco.tools.projects import (
    ProjectRecord,
    ProjectStore,
    StorageStatus,
    aiter_zip_workspace,
    is_runtime_secret_path,
)
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from ..auth import current_owner_id, current_session, require_admin_session
from ..runtime import ConversationRuntime
from ..title_service import fallback_title
from ._common import require_owned_conversation

_MAX_IMPORT_ZIP_BYTES = 50 * 1024 * 1024
_MAX_IMPORT_TREE_BYTES = 200 * 1024 * 1024
_MAX_IMPORT_FILES = 2000
_GIT_CLONE_TIMEOUT_S = 120


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
        uploads = [value for _key, value in form.multi_items() if isinstance(value, UploadFile)]
        if len(uploads) != 1:
            _reject_import(400, "invalid_request", "multipart import requires exactly one zip file")
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
    runtime.set_surface(conversation_id, "build")
    created_at = await _created_at_for(store, conversation_id, owner_id)
    ps.write_manifest(
        conversation_id,
        title=title,
        owner_id=owner_id,
        created_at=created_at,
        file_count=stats.files,
        total_bytes=stats.bytes,
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


def _zip_workspace_with_overlay(src: Path, overlay: dict[str, str]) -> Iterator[bytes]:
    """Stream a zip of the workspace tree PLUS the generated self-host overlay.

    The workspace portion is emitted identically to ``zip_workspace`` (same sorted
    order, same ZIP_DEFLATED, same runtime-secret exclusion), so a candidate
    download's SOURCE bytes are unchanged from the plain zip and only the overlay
    files are added. A workspace file always wins a path collision (the overlay
    entry is skipped), and a runtime-secret path is never emitted from the overlay
    — defense in depth over the caller's already-filtered map."""
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"workspace directory not found: {src}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        workspace_rels: set[str] = set()
        for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(src).as_posix()
            if is_runtime_secret_path(rel):
                continue
            zf.write(path, arcname=rel)
            workspace_rels.add(rel)
        for rel in sorted(overlay):
            if rel in workspace_rels or is_runtime_secret_path(rel):
                continue
            zf.writestr(rel, overlay[rel].encode("utf-8"))
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


def make_projects_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    async def list_projects(
        request: Request,
    ) -> dict:
        """List Build projects under the configured projects_root, joined with
        their conversation metadata (title/created_at). Returns an empty list
        with a clear `status` field when the storage isn't configured/valid —
        graceful empty, never crash."""
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
        owner_id = current_owner_id(request)
        if runtime is None:
            return {"titled": {}, "scanned": 0, "status": "no-runtime"}
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        candidates = [s.conversation_id for s in summaries if not s.title or retitle_fallbacks]
        titled = await runtime.title_service().backfill(
            candidates, retitle_fallbacks=retitle_fallbacks
        )
        return {"titled": titled, "count": len(titled), "scanned": len(candidates)}

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
    async def download_project(conversation_id: str, request: Request) -> StreamingResponse:
        """Stream a zip of the project's workspace. 404 with a specific reason
        when the storage is unconfigured / the project is unknown / the files
        have been deleted under the manifest.

        WO-7 upgrade: when the project assesses ``candidate`` and validation passes,
        the zip ADDITIONALLY carries the generated self-host overlay (``compose.yaml``,
        ``Dockerfile``(s), ``.dockerignore``, ``.env.example``, ``SELFHOST.md``,
        ``release.json``). A workspace file wins any path collision. For every other
        project the zip is byte-for-byte the plain filtered workspace zip — no
        overlay, no false affordance. The assessment is best-effort: any failure
        falls back to the plain zip so it can never break a download."""
        ps, record, workspace = await _resolve_project_for_read(
            request, store, runtime, conversation_id
        )
        overlay_files: dict[str, str] = {}
        try:
            from .release import assess_project

            overlay_files = assess_project(
                ps, record, workspace, record.conversation_id
            ).overlay_files
        except Exception:  # assessment must never break the plain download
            overlay_files = {}
        headers = {
            "Content-Disposition": (f'attachment; filename="{record.conversation_id}.zip"'),
        }
        body = (
            _aiter_zip_with_overlay(workspace, overlay_files)
            if overlay_files
            else aiter_zip_workspace(workspace)
        )
        return StreamingResponse(
            body,
            media_type="application/zip",
            headers=headers,
        )

    @router.get("/api/projects/{conversation_id}/manifest")
    async def project_manifest(conversation_id: str, request: Request) -> dict:
        """Export a JSON manifest of the project: metadata, the file tree (path +
        bytes), and the agent's last deliverable handoff (title/path/kind +
        deployment_url). The honest, portable description of what the run produced —
        the companion to the workspace zip download."""
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
        # file tree (workspace-relative path + size), skipping the codeact scratch files
        files: list[dict] = []
        workspace = ps.path_for(conversation_id)
        if workspace and workspace.is_dir():
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
            for e in reversed(await store.get_events(conversation_id)):
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
            "file_count": record.file_count,
            "total_bytes": record.total_bytes,
            "files": files,
            "deliverable": deliverable,
        }

    @router.delete("/api/projects/{conversation_id}")
    async def delete_project(conversation_id: str, request: Request) -> dict:
        """Remove a project's manifest + workspace from disk. The conversation
        events in SQLite are left alone (deleting those is a separate concern,
        and matches the History surface's existing delete semantics)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
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
        deleted = ps.delete(conversation_id)
        return {"id": conversation_id, "deleted": deleted}

    return router

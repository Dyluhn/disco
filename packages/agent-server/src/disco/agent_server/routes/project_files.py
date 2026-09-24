"""Read-only Files inspector bytes from the same current authority as downloads."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, is_runtime_secret_path
from fastapi import APIRouter, HTTPException, Request, Response

from ..runtime import ConversationRuntime
from ._common import require_owned_conversation
from .projects_download import _consumer_workspace, _owner_scoped_record, _project_store

_MAX_FILE_BYTES = 1024 * 1024


def _file_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        raise HTTPException(status_code=404, detail={"reason": "file_not_found"})
    normalized = PurePosixPath(path).as_posix()
    if is_runtime_secret_path(normalized):
        raise HTTPException(status_code=404, detail={"reason": "file_not_found"})
    return normalized


def _read_mirror(workspace: Path, path: str) -> bytes:
    """Anchor each component to an open directory; never traverse symlinks."""
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(workspace, flags)
        stack.callback(os.close, directory)
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            directory = os.open(part, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError(path)
        if info.st_size > _MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail={"reason": "file_too_large"})
        chunks = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail={"reason": "file_too_large"})
        return data


async def handle_project_file(
    conversation_id: str,
    path: str,
    request: Request,
    response: Response,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict:
    path = _file_path(path)
    project_store = _project_store(runtime)
    _owner_scoped_record(project_store, conversation_id, request)
    workspace, committed, _, _ = await _consumer_workspace(store, project_store, conversation_id)
    try:
        if committed is not None:
            with project_store.open_verified_version(conversation_id, committed.seq) as verified:
                entry = next((item for item in verified.files if item.path == path), None)
                if entry is None:
                    raise FileNotFoundError(path)
                if entry.size > _MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail={"reason": "file_too_large"})
                data = verified.read_bytes(path, max_bytes=_MAX_FILE_BYTES)
        else:
            assert workspace is not None
            data = _read_mirror(workspace, path)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"reason": "workspace_unsealed"}) from exc
    except OSError as exc:
        raise HTTPException(status_code=404, detail={"reason": "file_not_found"}) from exc
    try:
        content = data.decode("utf-8") if b"\x00" not in data else None
    except UnicodeDecodeError:
        content = None
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "path": path,
        "content": content,
        "binary": content is None,
        "bytes": len(data),
        "version_seq": committed.seq if committed is not None else None,
    }


def register_project_file_route(
    router: APIRouter,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.get("/api/projects/{conversation_id}/files/{path:path}")
    async def project_file(
        conversation_id: str,
        path: str,
        request: Request,
        response: Response,
    ) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await handle_project_file(
            conversation_id,
            path,
            request,
            response,
            store=store,
            runtime=runtime,
        )

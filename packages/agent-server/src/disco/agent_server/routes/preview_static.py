"""Immutable and legacy-static preview response mechanics."""

from __future__ import annotations

import mimetypes
import urllib.parse
from pathlib import PurePosixPath
from typing import Any

from disco.core import (
    DeliverableEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
)
from disco.core.auth import PreviewCapability
from disco.core.events import Event
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS
from disco.tools.sandbox.base import strip_redundant_workspace_prefix
from fastapi import Request, Response

from ..preview_inject import inject_element_mention_picker, inject_selection_agent
from ..preview_paths import safe_preview_path as _safe_preview_path
from ..preview_projection import (
    SealedPreviewRuntimeContract,
    derive_sealed_preview_runtime_contract,
)
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from .preview_browser import (
    _canonical_preview_port,
    _fetch_inside_response,
    _upstream_http_response,
    _wake_for_preview,
)


def _snapshot_request_path(
    workspace: Any,
    requested_path: str,
    entry_path: str | None,
) -> Any | None:
    requested = _safe_preview_path(requested_path, allow_leading_slash=True)
    if requested is None:
        return None

    if entry_path is None:
        rel = requested or "index.html"
        return (workspace / rel).resolve()

    normalized_entry = strip_redundant_workspace_prefix(entry_path.strip())
    entry = _safe_preview_path(normalized_entry, allow_leading_slash=False)
    if entry is None:
        return None
    if entry in {"", "."}:
        entry = "index.html"

    selected = (workspace / entry).resolve()
    if not selected.is_relative_to(workspace):
        return None
    if selected.is_dir():
        base = PurePosixPath(entry)
        selected = (selected / "index.html").resolve()
    else:
        base = PurePosixPath(entry).parent

    if not selected.is_file():
        return None
    if not requested:
        return selected

    base_text = "" if str(base) == "." else str(base)
    if base_text and (requested == base_text or requested.startswith(f"{base_text}/")):
        rel = requested
    else:
        rel = f"{base_text}/{requested}" if base_text else requested
    return (workspace / rel).resolve()


def _verified_snapshot_request_path(
    files: set[str],
    requested_path: str,
    entry_path: str | None,
) -> str | None:
    requested = _safe_preview_path(requested_path, allow_leading_slash=True)
    if requested is None:
        return None
    directories = _verified_directories(files)
    if entry_path is None:
        return _verified_default_request(files, directories, requested)
    return _verified_entry_request(files, directories, requested, entry_path)


def _verified_directories(files: set[str]) -> set[str]:
    return {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in files
        for index in range(1, len(PurePosixPath(path).parts))
    }


def _verified_default_request(
    files: set[str],
    directories: set[str],
    requested: str,
) -> str | None:
    selected = requested or "index.html"
    if selected in directories:
        selected = f"{selected}/index.html"
    return selected if selected in files else None


def _verified_entry_request(
    files: set[str],
    directories: set[str],
    requested: str,
    entry_path: str,
) -> str | None:
    normalized_entry = strip_redundant_workspace_prefix(entry_path.strip())
    entry = _safe_preview_path(normalized_entry, allow_leading_slash=False)
    if entry is None:
        return None
    entry = "index.html" if entry in {"", "."} else entry
    if entry in directories:
        selected = f"{entry}/index.html"
        base = PurePosixPath(entry)
    else:
        selected = entry
        base = PurePosixPath(entry).parent
    if selected not in files:
        return None
    if not requested:
        return selected
    base_text = "" if str(base) == "." else base.as_posix()
    candidate = _entry_relative_request(base_text, requested)
    if candidate in directories:
        candidate = f"{candidate}/index.html"
    return candidate if candidate in files else None


def _entry_relative_request(base_text: str, requested: str) -> str:
    if base_text and (requested == base_text or requested.startswith(f"{base_text}/")):
        return requested
    return f"{base_text}/{requested}" if base_text else requested


def _selected_app_entry(
    events: list[Any],
    *,
    version: int | None,
    marker_seq: int | None = None,
) -> str | None:
    public_markers = _public_version_markers(events, version)
    marker_seq = _selected_marker_seq(public_markers, version, marker_seq)
    if version is not None and marker_seq is None:
        return None
    return _last_app_entry_before(events, marker_seq)


def _public_version_markers(
    events: list[Any],
    version: int | None,
) -> list[WorkspaceVersionEvent]:
    return [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent)
        and event.version_seq == version
        and event.seq is not None
        and not (event.final_seal is None and event.trigger.startswith("finalizing:"))
    ]


def _selected_marker_seq(
    public_markers: list[WorkspaceVersionEvent],
    version: int | None,
    marker_seq: int | None,
) -> int | None:
    if version is not None and marker_seq is None:
        marker_seq = max(
            (event.seq for event in public_markers if event.seq is not None),
            default=None,
        )
    if version is not None and marker_seq is None:
        return None
    if version is not None and not any(event.seq == marker_seq for event in public_markers):
        return None
    return marker_seq


def _last_app_entry_before(
    events: list[Any],
    marker_seq: int | None,
) -> str | None:
    selected: str | None = None
    for event in events:
        if marker_seq is not None and event.seq is not None and event.seq > marker_seq:
            break
        if isinstance(event, DeliverableEvent) and event.artifact_kind == "app":
            selected = event.path
    return selected


def _finished_snapshot_is_committed(events: list[Any]) -> bool:
    try:
        terminal_seq, latest_effect_seq = derive_final_workspace_fence(events)
    except ValueError:
        return False
    candidates = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent)
        and event.final_seal is not None
        and event.final_seal.terminal_seq == terminal_seq
        and event.final_seal.latest_effect_seq == latest_effect_seq
        and (event.seq or -1) > terminal_seq
    ]
    if not candidates:
        return False
    latest = max(candidates, key=lambda event: event.seq or -1)
    return not any(
        isinstance(event, WorkspaceVersionEvent) and (event.seq or -1) > (latest.seq or -1)
        for event in events
    )


def _serve_static_from_snapshot(
    runtime: ConversationRuntime,
    conversation_id: str,
    rel_path: str,
    *,
    version: int | None = None,
    entry_path: str | None = None,
    inject_selection: bool = False,
    committed: bool = False,
) -> Response | None:
    if is_runtime_secret_path(rel_path):
        return None
    try:
        project_store = runtime.projects.current_project_store()
        if project_store is None or project_store.status() != StorageStatus.OK:
            return None
        if version is not None:
            return _verified_static_response(
                project_store,
                conversation_id,
                version,
                rel_path,
                entry_path=entry_path,
                inject_selection=inject_selection,
            )
        workspace = project_store.path_for(conversation_id).resolve()
    except StorageError:
        if version is not None:
            return Response(
                "committed workspace unavailable" if committed else "version not found",
                status_code=503 if committed else 404,
                media_type="text/plain",
            )
        return None
    except Exception:  # noqa: BLE001 — no snapshot lets the caller fail honestly
        return None
    return _mutable_static_response(
        workspace,
        rel_path,
        entry_path=entry_path,
        inject_selection=inject_selection,
    )


def _verified_static_response(
    project_store: Any,
    conversation_id: str,
    version: int,
    rel_path: str,
    *,
    entry_path: str | None,
    inject_selection: bool,
) -> Response | None:
    with project_store.open_verified_version(conversation_id, version) as verified:
        target_rel = _verified_snapshot_request_path(
            {entry.path for entry in verified.files},
            rel_path,
            entry_path,
        )
        if target_rel is None:
            return _missing_static_response(entry_path, "preview asset not found")
        body = verified.read_bytes(target_rel)
        content_type = mimetypes.guess_type(target_rel)[0] or "application/octet-stream"
    return _static_bytes_response(body, content_type, inject_selection)


def _missing_static_response(
    entry_path: str | None,
    message: str,
) -> Response | None:
    if entry_path is None:
        return None
    return Response(message, status_code=404, media_type="text/plain")


def _static_bytes_response(
    body: bytes,
    content_type: str,
    inject_selection: bool,
) -> Response:
    if inject_selection:
        body = inject_selection_agent(body, content_type)
    return Response(
        content=inject_element_mention_picker(body, content_type),
        media_type=content_type,
    )


def _mutable_static_response(
    workspace: Any,
    rel_path: str,
    *,
    entry_path: str | None,
    inject_selection: bool,
) -> Response | None:
    target = _snapshot_request_path(workspace, rel_path, entry_path)
    if target is None:
        return _missing_static_response(entry_path, "preview target not found")
    if target.is_dir():
        target = (target / "index.html").resolve()
    if (
        not target.is_relative_to(workspace)
        or is_runtime_secret_path(target.relative_to(workspace).as_posix())
        or not target.is_file()
    ):
        return _missing_static_response(entry_path, "preview asset not found")
    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    body = target.read_bytes()
    return _static_bytes_response(body, content_type, inject_selection)


async def _committed_static_capability_available(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    workspace_version: int | None = None,
) -> bool:
    if runtime is None:
        return False
    version = workspace_version
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing evidence cannot authorize fallback
        return False
    if version is None:
        project_store = runtime.projects.current_project_store()
        if project_store is None or project_store.status() != StorageStatus.OK:
            return False
        try:
            committed = resolve_committed_workspace(
                events,
                project_store,
                conversation_id,
            )
        except WorkspaceCommitUnavailable:
            return False
        version = committed.event.version_seq
        marker_seq = committed.event.seq
    else:
        marker_seq = None
    entry_path = _selected_app_entry(
        events,
        version=version,
        marker_seq=marker_seq,
    )
    if entry_path is None:
        return False
    served = _serve_static_from_snapshot(
        runtime,
        conversation_id,
        "",
        version=version,
        entry_path=entry_path,
        inject_selection=False,
    )
    return served is not None and served.status_code < 400


def _sealed_runtime_contract(
    events: list[Event],
    conversation_id: str,
    committed: Any,
    entry: str,
) -> SealedPreviewRuntimeContract | None:
    seal = committed.event.final_seal
    if seal is None:
        return None
    return derive_sealed_preview_runtime_contract(
        events,
        terminal_seq=seal.terminal_seq,
        conversation_id=conversation_id,
        version_seq=committed.event.version_seq,
        tree_digest=committed.event.tree_digest,
        app_entry=entry,
    )


def _historical_preview_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    safe_path: str,
    events: list[Event],
    workspace_version: int,
    *,
    inject_selection: bool,
) -> Response:
    entry_path = _selected_app_entry(events, version=workspace_version)
    served = _serve_static_from_snapshot(
        runtime,
        conversation_id,
        safe_path,
        version=workspace_version,
        entry_path=entry_path,
        inject_selection=inject_selection,
    )
    if served is not None:
        return served
    if entry_path is not None:
        return Response(
            "committed preview unavailable",
            status_code=503,
            media_type="text/plain",
        )
    return Response(
        "version not found",
        status_code=404,
        media_type="text/plain",
    )


def _forwarded_preview_query(request: Request) -> str:
    pairs = urllib.parse.parse_qsl(
        request.url.query,
        keep_blank_values=True,
    )
    return urllib.parse.urlencode(pairs, doseq=True)


async def _serve_active_finished_projection(
    runtime: ConversationRuntime,
    conversation_id: str,
    rel_path: str,
    contract: SealedPreviewRuntimeContract,
    *,
    capability_port: int | None,
    inject_selection: bool = False,
) -> Response | None:
    resolved = await runtime.preview.resolve_finished_preview_runtime(
        conversation_id,
        contract,
    )
    if not isinstance(resolved, dict):
        return None
    port = resolved.get("port")
    if (
        type(port) is not int
        or (port not in USER_PORTS and not is_managed_host_preview_port(port))
        or port == NOVNC_PORT
    ):
        return None
    if capability_port is not None and capability_port != port:
        return None
    upstream = runtime.preview.port_upstream(conversation_id, port)
    if upstream is None:
        return await _fetch_inside_response(
            runtime,
            conversation_id,
            port,
            rel_path,
            inject_selection=inject_selection,
        )
    return await _upstream_http_response(
        runtime,
        conversation_id,
        port,
        upstream,
        rel_path,
        inject_selection=inject_selection,
    )


async def _finished_preview_response(
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    safe_path: str,
    events: list[Event],
    preview_cap: PreviewCapability | None,
) -> Response:
    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        return Response(
            "finished workspace is not sealed",
            status_code=503,
            media_type="text/plain",
        )
    try:
        committed = resolve_committed_workspace(
            events,
            project_store,
            conversation_id,
        )
    except WorkspaceCommitUnavailable:
        return Response(
            "finished workspace is finalizing or unsealed",
            status_code=503,
            media_type="text/plain",
        )
    entry_path = _selected_app_entry(
        events,
        version=committed.event.version_seq,
        marker_seq=committed.event.seq,
    )
    contract = (
        _sealed_runtime_contract(
            events,
            conversation_id,
            committed,
            entry_path,
        )
        if entry_path is not None
        else None
    )
    if contract is not None:
        return await _finished_runtime_response(
            runtime,
            request,
            conversation_id,
            safe_path,
            contract,
            preview_cap,
        )
    served = _serve_static_from_snapshot(
        runtime,
        conversation_id,
        safe_path,
        version=committed.event.version_seq,
        entry_path=entry_path,
        inject_selection=preview_cap is not None,
        committed=True,
    )
    if served is not None:
        return served
    message = (
        "committed preview unavailable"
        if entry_path is not None
        else "committed preview target unavailable"
    )
    return Response(message, status_code=503, media_type="text/plain")


async def _finished_runtime_response(
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    safe_path: str,
    contract: SealedPreviewRuntimeContract,
    preview_cap: PreviewCapability | None,
) -> Response:
    query = _forwarded_preview_query(request)
    active_path = f"{safe_path}?{query}" if query else safe_path
    async with runtime.preview.capture_lease(conversation_id):
        active = await _serve_active_finished_projection(
            runtime,
            conversation_id,
            active_path,
            contract,
            capability_port=(preview_cap.port if preview_cap is not None else None),
            inject_selection=preview_cap is not None,
        )
    if active is not None:
        return active
    return Response(
        "sealed runtime preview unavailable",
        status_code=503,
        media_type="text/plain",
    )


async def _active_preview_response(
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    safe_path: str,
    events: list[Event],
    preview_cap: PreviewCapability | None,
    owner_id: str,
) -> Response:
    target_port = (
        preview_cap.port
        if preview_cap is not None
        else _canonical_preview_port(runtime, conversation_id)
    )
    cid8 = conversation_id.removeprefix("conv_")[:8]
    upstream = (
        await _wake_for_preview(
            runtime,
            cid8,
            target_port,
            owner_id=owner_id,
        )
        if target_port is not None
        else None
    )
    query = _forwarded_preview_query(request)
    upstream_path = f"{safe_path}?{query}" if query else safe_path
    if upstream is None:
        served = (
            await _fetch_inside_response(
                runtime,
                conversation_id,
                target_port,
                upstream_path,
                inject_selection=preview_cap is not None,
            )
            if target_port is not None
            else None
        )
        if served is not None:
            return served
        return Response(
            "preview not available",
            status_code=503,
            media_type="text/plain",
        )
    assert target_port is not None
    return await _active_upstream_response(
        runtime,
        conversation_id,
        target_port,
        upstream,
        safe_path,
        query,
        preview_cap,
    )


async def _active_upstream_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    target_port: int,
    upstream: str,
    safe_path: str,
    query: str,
    preview_cap: PreviewCapability | None,
) -> Response:
    upstream_path = f"{safe_path}?{query}" if query else safe_path
    served = await _upstream_http_response(
        runtime,
        conversation_id,
        target_port,
        upstream.rstrip("/"),
        upstream_path,
        inject_selection=preview_cap is not None,
    )
    if served is not None:
        return served
    return Response(
        "preview upstream unreachable",
        status_code=502,
        media_type="text/plain",
    )

"""Preview HTTP and WebSocket proxy mechanics."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

import websockets
from disco.core import ConversationStatus, StatusEvent
from disco.core.auth import (
    PreviewCapability,
    PreviewCapabilitySigner,
    cookie_header_from_headers,
    path_preview_cookie_name,
)
from disco.core.events import Event
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus, is_runtime_secret_path
from fastapi import HTTPException, Request, Response, WebSocket

from ..auth import current_session, websocket_session
from ..preview_paths import safe_preview_path as _safe_preview_path
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import (
    require_owned_conversation,
    require_owned_conversation_for_owner,
)
from .preview_browser import (
    _canonical_preview_port,
    _fetch_inside_response,
    _path_preview_port_from_label,
    _preview_websocket_origin_allowed,
    _upstream_http_response,
    _wake_for_preview,
)
from .preview_capability import (
    _canonical_preview_authority,
)
from .preview_static import (
    _active_preview_response,
    _finished_preview_response,
    _historical_preview_response,
    _sealed_runtime_contract,
    _selected_app_entry,
)

_LOG = logging.getLogger(__name__)


async def _path_preview_websocket_capability_owner(
    websocket: WebSocket,
    store: SqliteEventStore,
    conversation_id: str,
) -> tuple[str | None, str, int | None]:
    if not _preview_websocket_origin_allowed(
        websocket.headers.get("origin"),
        websocket.headers.get("host"),
        websocket_scheme=websocket.url.scheme,
        forwarded_proto=websocket.headers.get("x-forwarded-proto"),
    ):
        return None, "preview origin required", None
    cid8 = conversation_id.removeprefix("conv_")[:8]
    request_label = (websocket.url.hostname or "").split(".", 1)[0].lower()
    port = _path_preview_port_from_label(conversation_id, request_label)
    if port is None:
        return None, "preview capability required", None
    try:
        cookie_name = path_preview_cookie_name(cid8)
    except ValueError:
        return None, "preview capability required", None
    capability = PreviewCapabilitySigner().verify_cookie_header(
        cookie_header_from_headers(websocket.headers),
        cookie_name,
        cid8=cid8,
        port=port,
        method="WEBSOCKET",
        path=websocket.url.path,
    )
    if capability is None or capability.conversation_id != conversation_id:
        return None, "preview capability required", None
    owner_id = await store.conversation_owner_id(conversation_id)
    if owner_id is None:
        return None, "conversation not found", None
    if owner_id != capability.owner_id:
        return None, "conversation forbidden", None
    return owner_id, "", port


def _legacy_workspace_version(request: Request) -> int | None:
    values = request.query_params.getlist("version")
    if not values:
        return None
    if len(values) != 1:
        raise HTTPException(
            status_code=422,
            detail="invalid workspace version",
        )
    try:
        version = int(values[0])
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid workspace version",
        ) from exc
    if version < 1:
        raise HTTPException(
            status_code=422,
            detail="invalid workspace version",
        )
    return version


async def _close_ws(
    websocket: WebSocket,
    code: int,
    reason: str,
) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


async def _proxy_websocket_to_upstream(
    websocket: WebSocket,
    upstream: str,
    rel_path: str,
) -> None:
    upstream_parsed = urllib.parse.urlparse(upstream)
    ws_scheme = "wss" if upstream_parsed.scheme in {"https", "wss"} else "ws"
    target_path = "/" + rel_path.lstrip("/")
    query_string = websocket.scope.get("query_string", b"").decode("latin1")
    target_url = urllib.parse.urlunparse(
        (
            ws_scheme,
            upstream_parsed.netloc,
            target_path,
            "",
            query_string,
            "",
        )
    )
    subprotocols = _websocket_subprotocols(websocket)
    try:
        ws_client = await _connect_upstream_websocket(
            target_url,
            subprotocols,
        )
    except Exception as exc:  # noqa: BLE001 — failed upgrade closes
        _LOG.warning("preview websocket upstream connect error: %s", exc)
        await _close_ws(
            websocket,
            1011,
            "preview upstream unreachable",
        )
        return
    await websocket.accept(subprotocol=ws_client.subprotocol)
    await _bridge_websockets(websocket, ws_client)


def _websocket_subprotocols(websocket: WebSocket) -> list[Any]:
    from websockets.typing import Subprotocol

    subprotocols = [Subprotocol(protocol) for protocol in websocket.scope.get("subprotocols", [])]
    if not subprotocols:
        header = websocket.headers.get("sec-websocket-protocol")
        if header:
            subprotocols = [
                Subprotocol(protocol.strip()) for protocol in header.split(",") if protocol.strip()
            ]
    return subprotocols


async def _connect_upstream_websocket(
    target_url: str,
    subprotocols: list[Any],
) -> Any:
    if subprotocols:
        return await websockets.connect(
            target_url,
            subprotocols=subprotocols,
        )
    return await websockets.connect(target_url)


async def _bridge_websockets(
    websocket: WebSocket,
    ws_client: Any,
) -> None:
    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.receive":
                    if "text" in message:
                        await ws_client.send(message["text"])
                    elif "bytes" in message:
                        await ws_client.send(message["bytes"])
                elif message["type"] == "websocket.disconnect":
                    await ws_client.close(message.get("code", 1000))
                    break
        except Exception:  # noqa: BLE001 — peer/upstream closed
            with contextlib.suppress(Exception):
                await ws_client.close(1011)

    async def upstream_to_client() -> None:
        try:
            async for message in ws_client:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)
            await _close_ws(websocket, 1000, "")
        except websockets.ConnectionClosed as exc:
            await _close_ws(websocket, exc.code, exc.reason)
        except Exception:  # noqa: BLE001 — downstream disconnected
            await _close_ws(websocket, 1011, "preview websocket failed")

    client_task = asyncio.create_task(client_to_upstream())
    upstream_task = asyncio.create_task(upstream_to_client())
    try:
        _done, pending = await asyncio.wait(
            [client_task, upstream_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        with contextlib.suppress(Exception):
            await ws_client.close()


async def _canonical_preview_workspace_version(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    conversation_id: str,
    request: Request,
    preview_cap: object | None,
) -> tuple[int | None, Response | None]:
    canonical = (
        preview_cap
        if (isinstance(preview_cap, PreviewCapability) and preview_cap.authority_id is not None)
        else None
    )
    if canonical is None:
        return _legacy_workspace_version(request), None
    authority = await _canonical_preview_authority(
        store,
        runtime,
        conversation_id,
        canonical.port,
        workspace_version=canonical.immutable_version,
    )
    if authority != canonical.authority_id:
        return canonical.immutable_version, Response(
            "preview generation changed",
            status_code=409,
            media_type="text/plain",
        )
    return canonical.immutable_version, None


async def _port_app_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    port: int,
    path: str,
    owner_id: str,
) -> Response:
    cid8 = conversation_id.removeprefix("conv_")[:8]
    upstream = await _wake_for_preview(
        runtime,
        cid8,
        port,
        owner_id=owner_id,
    )
    if upstream is None:
        served = await _fetch_inside_response(
            runtime,
            conversation_id,
            port,
            path,
        )
        if served is not None:
            return served
        return Response(
            "preview not available",
            status_code=503,
            media_type="text/plain",
        )
    served = await _upstream_http_response(
        runtime,
        conversation_id,
        port,
        upstream,
        path,
        inject_selection=False,
    )
    if served is not None:
        return served
    return Response(
        "preview upstream unreachable",
        status_code=502,
        media_type="text/plain",
    )


async def _preview_app_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    request: Request,
    conversation_id: str,
    path: str,
) -> Response:
    preview_cap = getattr(request.state, "preview_capability", None)
    if preview_cap is not None and not isinstance(preview_cap, PreviewCapability):
        return Response(
            "invalid preview capability",
            status_code=403,
            media_type="text/plain",
        )
    if preview_cap is None:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        owner_id = current_session(request).owner_id
    else:
        if preview_cap.conversation_id != conversation_id:
            return Response(
                "preview capability mismatch",
                status_code=403,
                media_type="text/plain",
            )
        owner_id = preview_cap.owner_id
    if runtime is None:
        return Response(
            "preview not available",
            status_code=503,
            media_type="text/plain",
        )
    safe_path = _safe_preview_path(path, allow_leading_slash=True)
    if safe_path is None or is_runtime_secret_path(safe_path):
        return Response(
            "preview path not found",
            status_code=404,
            media_type="text/plain",
        )
    try:
        if preview_cap is not None:
            # The capability's generation is minted in the preceding request,
            # while this request performs the potentially slow finished-preview
            # restore and body read.  Hold the same conversation lock across the
            # whole redemption transaction so lifecycle teardown cannot detach
            # the backing sandbox between target resolution and upstream read.
            async with runtime.preview.capture_lease(conversation_id):
                return await _resolved_preview_response(
                    store,
                    runtime,
                    request,
                    conversation_id,
                    safe_path,
                    preview_cap,
                    owner_id,
                )
        return await _resolved_preview_response(
            store,
            runtime,
            request,
            conversation_id,
            safe_path,
            preview_cap,
            owner_id,
        )
    finally:
        if preview_cap is not None:
            complete_capture = getattr(runtime.preview, "complete_capture", None)
            if callable(complete_capture):
                capture_generation = getattr(preview_cap, "capture_generation", None)
                if capture_generation is None:
                    complete_capture(conversation_id)
                else:
                    complete_capture(conversation_id, capture_generation)


async def _resolved_preview_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    safe_path: str,
    preview_cap: PreviewCapability | None,
    owner_id: str,
) -> Response:
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — target metadata is authority
        _LOG.warning(
            "preview target metadata unavailable for %s",
            conversation_id,
            exc_info=True,
        )
        return Response(
            "preview metadata unavailable",
            status_code=503,
            media_type="text/plain",
        )
    workspace_version, authority_error = await _canonical_preview_workspace_version(
        store,
        runtime,
        conversation_id,
        request,
        preview_cap,
    )
    if authority_error is not None:
        return authority_error
    if workspace_version is not None:
        return _historical_preview_response(
            runtime,
            conversation_id,
            safe_path,
            events,
            workspace_version,
            inject_selection=preview_cap is not None,
        )
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        return await _finished_preview_response(
            runtime,
            request,
            conversation_id,
            safe_path,
            events,
            preview_cap,
        )
    return await _active_preview_response(
        runtime,
        request,
        conversation_id,
        safe_path,
        events,
        preview_cap,
        owner_id,
    )


@dataclass(frozen=True)
class _WebSocketPreviewIdentity:
    conversation_id: str
    owner_id: str
    target_port: int | None
    canonical_capability: PreviewCapability | None


async def _canonical_websocket_identity(
    websocket: WebSocket,
    store: SqliteEventStore,
    conversation_id: str,
    capability: PreviewCapability,
) -> _WebSocketPreviewIdentity | None:
    if capability.conversation_id != conversation_id:
        await _close_ws(
            websocket,
            1008,
            "preview capability mismatch",
        )
        return None
    stored_owner = await store.conversation_owner_id(conversation_id)
    if stored_owner is None or stored_owner != capability.owner_id:
        await _close_ws(websocket, 1008, "conversation forbidden")
        return None
    return _WebSocketPreviewIdentity(
        conversation_id=conversation_id,
        owner_id=capability.owner_id,
        target_port=capability.port,
        canonical_capability=capability,
    )


async def _session_websocket_identity(
    websocket: WebSocket,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    session: Any,
    raw_capability: PreviewCapability | None,
) -> _WebSocketPreviewIdentity | None:
    try:
        conversation_id = await require_owned_conversation_for_owner(
            store,
            conversation_id,
            session.owner_id,
            owner_bypass=session.session_id == "test-session",
        )
    except HTTPException:
        await _close_ws(websocket, 1008, "conversation forbidden")
        return None
    return _WebSocketPreviewIdentity(
        conversation_id=conversation_id,
        owner_id=session.owner_id,
        target_port=(_canonical_preview_port(runtime, conversation_id) if runtime else None),
        canonical_capability=raw_capability,
    )


async def _path_websocket_identity(
    websocket: WebSocket,
    store: SqliteEventStore,
    conversation_id: str,
    raw_capability: PreviewCapability | None,
) -> _WebSocketPreviewIdentity | None:
    owner_id, error, port = await _path_preview_websocket_capability_owner(
        websocket,
        store,
        conversation_id,
    )
    if owner_id is None:
        await _close_ws(websocket, 1008, error)
        return None
    return _WebSocketPreviewIdentity(
        conversation_id=conversation_id,
        owner_id=owner_id,
        target_port=port,
        canonical_capability=raw_capability,
    )


async def _websocket_preview_identity(
    websocket: WebSocket,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
) -> _WebSocketPreviewIdentity | None:
    canonical_cap = websocket.scope.get("state", {}).get("canonical_preview_capability")
    raw_capability = canonical_cap if isinstance(canonical_cap, PreviewCapability) else None
    session = websocket_session(websocket)
    if isinstance(canonical_cap, PreviewCapability) and canonical_cap.authority_id is not None:
        return await _canonical_websocket_identity(
            websocket,
            store,
            conversation_id,
            canonical_cap,
        )
    if session is None:
        return await _path_websocket_identity(
            websocket,
            store,
            conversation_id,
            raw_capability,
        )
    return await _session_websocket_identity(
        websocket,
        store,
        runtime,
        conversation_id,
        session,
        raw_capability,
    )


async def _canonical_websocket_authority_valid(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    identity: _WebSocketPreviewIdentity,
) -> bool:
    capability = identity.canonical_capability
    if capability is None:
        return True
    current_authority = await _canonical_preview_authority(
        store,
        runtime,
        identity.conversation_id,
        capability.port,
        workspace_version=capability.immutable_version,
    )
    return current_authority == capability.authority_id


async def _finished_websocket_upstream(
    runtime: ConversationRuntime,
    events: list[Event],
    identity: _WebSocketPreviewIdentity,
) -> str | None:
    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        return None
    try:
        committed = resolve_committed_workspace(
            events,
            project_store,
            identity.conversation_id,
        )
    except WorkspaceCommitUnavailable:
        return None
    entry = _selected_app_entry(
        events,
        version=committed.event.version_seq,
        marker_seq=committed.event.seq,
    )
    contract = (
        _sealed_runtime_contract(
            events,
            identity.conversation_id,
            committed,
            entry,
        )
        if entry is not None
        else None
    )
    resolved = (
        await runtime.preview.resolve_finished_preview_runtime(
            identity.conversation_id,
            contract,
        )
        if contract is not None
        else None
    )
    runtime_port = resolved.get("port") if isinstance(resolved, dict) else None
    if contract is None or type(runtime_port) is not int or identity.target_port != runtime_port:
        return None
    return runtime.preview.port_upstream(
        identity.conversation_id,
        runtime_port,
    )


async def _active_websocket_upstream(
    runtime: ConversationRuntime,
    identity: _WebSocketPreviewIdentity,
) -> str | None:
    if identity.target_port is None:
        return None
    cid8 = identity.conversation_id.removeprefix("conv_")[:8]
    return await _wake_for_preview(
        runtime,
        cid8,
        identity.target_port,
        owner_id=identity.owner_id,
    )


async def _preview_app_websocket_response(
    websocket: WebSocket,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    path: str,
) -> None:
    identity = await _websocket_preview_identity(
        websocket,
        store,
        runtime,
        conversation_id,
    )
    if identity is None:
        return
    if runtime is None:
        await _close_ws(websocket, 1008, "preview not available")
        return
    capability = identity.canonical_capability
    if capability is not None and capability.immutable_version is not None:
        await _close_ws(
            websocket,
            1008,
            "historical previews are static",
        )
        return
    try:
        events = await store.get_events(identity.conversation_id)
    except Exception:  # noqa: BLE001 — uncertain status cannot wake
        await _close_ws(
            websocket,
            1008,
            "preview metadata unavailable",
        )
        return
    if not await _canonical_websocket_authority_valid(
        store,
        runtime,
        identity,
    ):
        await _close_ws(
            websocket,
            1008,
            "preview generation changed",
        )
        return
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    finished = latest_status is not None and latest_status.status is ConversationStatus.FINISHED
    upstream = (
        await _finished_websocket_upstream(runtime, events, identity)
        if finished
        else await _active_websocket_upstream(runtime, identity)
    )
    if upstream is None:
        await _close_ws(websocket, 1008, "preview not available")
        return
    await _proxy_websocket_to_upstream(websocket, upstream, path)

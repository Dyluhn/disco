"""Preview capability, isolated-origin, and authority translation."""

from __future__ import annotations

import hashlib
import json as _json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from disco.core import ConversationStatus, Event, StatusEvent, WorkspaceVersionEvent
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    MAX_PREVIEW_TARGET_PATH_CHARS,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_BOOTSTRAP_PATH,
    AuthSession,
    PreviewCapabilitySigner,
    SessionSigner,
    cookie_header_from_headers,
    local_preview_gateway_ports,
    path_preview_cookie_name,
    preview_ttl_s,
)
from disco.core.env import disco_env
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import current_session
from ..preview_bootstrap import (
    MAX_PREVIEW_REDEMPTION_BODY_BYTES,
    cross_site_iframe_headers,
    parse_preview_redemption,
    preview_navigation_document,
    preview_redemption_content_type,
)
from ..preview_paths import (
    safe_capability_target_path as _safe_capability_target_path,
)
from ..preview_projection import SealedPreviewRuntimeContract
from ..runtime import ConversationRuntime
from ..workspace_commit import (
    CommittedWorkspaceView,
    WorkspaceCommitUnavailable,
    resolve_committed_workspace,
)
from ._common import require_owned_conversation
from .preview_browser import (
    _canonical_preview_port,
    _local_preview_bootstrap_url,
    _local_preview_request,
    _path_preview_bootstrap_url,
    _preview_bootstrap_url,
)
from .preview_finished import refresh_canonical_preview_port
from .preview_static import (
    _committed_static_capability_available,
    _sealed_runtime_contract,
    _selected_app_entry,
)


class PreviewCapabilityBody(BaseModel):
    port: int | None = None
    target_path: str = Field("/", max_length=MAX_PREVIEW_TARGET_PATH_CHARS)
    transport: Literal["host", "path", "path_live", "canonical"] = "host"
    workspace_version: int | None = Field(default=None, ge=1)


def _preview_authority_label(kind: str, *parts: object) -> str:
    material = _json.dumps(
        [kind, *parts],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _version_preview_authority(
    events: list[Event],
    conversation_id: str,
    version: int,
) -> str | None:
    candidates = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.version_seq == version
    ]
    if not candidates:
        return None
    version_event = max(candidates, key=lambda event: event.seq or -1)
    entry = _selected_app_entry(events, version=version)
    if entry is None:
        return None
    return _preview_authority_label(
        "version",
        conversation_id,
        version,
        version_event.tree_digest,
        entry,
    )


async def _sealed_preview_authority(
    runtime: ConversationRuntime,
    conversation_id: str,
    selected_port: int,
    committed: CommittedWorkspaceView,
    contract: SealedPreviewRuntimeContract,
    entry: str,
) -> str | None:
    resolved = await runtime.preview.resolve_finished_preview_runtime(
        conversation_id,
        contract,
    )
    if not isinstance(resolved, dict):
        return None
    runtime_port = resolved.get("port")
    generation = resolved.get("projection_id")
    if (
        type(runtime_port) is not int
        or runtime_port != selected_port
        or not isinstance(generation, str)
        or not generation.startswith("pv_")
    ):
        return None
    event = committed.event
    return _preview_authority_label(
        "sealed-runtime",
        conversation_id,
        event.version_seq,
        event.tree_digest,
        contract.contract_id,
        generation,
        runtime_port,
        entry,
    )


async def _finished_preview_authority(
    runtime: ConversationRuntime,
    events: list[Event],
    conversation_id: str,
    selected_port: int,
) -> str | None:
    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        return None
    try:
        committed = resolve_committed_workspace(
            events,
            project_store,
            conversation_id,
        )
    except WorkspaceCommitUnavailable:
        return None
    entry = _selected_app_entry(
        events,
        version=committed.event.version_seq,
        marker_seq=committed.event.seq,
    )
    if entry is None:
        return None
    contract = _sealed_runtime_contract(
        events,
        conversation_id,
        committed,
        entry,
    )
    if contract is not None:
        return await _sealed_preview_authority(
            runtime,
            conversation_id,
            selected_port,
            committed,
            contract,
            entry,
        )
    return _preview_authority_label(
        "committed",
        conversation_id,
        committed.event.version_seq,
        committed.event.tree_digest,
        entry,
    )


async def _live_preview_authority(
    runtime: ConversationRuntime,
    events: list[Event],
    conversation_id: str,
    selected_port: int,
) -> str | None:
    try:
        metadata = await runtime.preview.preview(conversation_id)
    except Exception:  # noqa: BLE001 — lifecycle authority cannot be guessed
        return None
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    metadata_port = metadata.get("port") if isinstance(metadata, dict) else None
    if not isinstance(generation, str) or not generation or metadata_port != selected_port:
        return None
    entry = _selected_app_entry(events, version=None)
    return _preview_authority_label(
        "live",
        conversation_id,
        generation,
        selected_port,
        entry or "",
    )


async def _canonical_preview_authority(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    selected_port: int,
    *,
    workspace_version: int | None = None,
) -> str | None:
    if runtime is None:
        return None
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing authority evidence fails closed
        return None
    if workspace_version is not None:
        return _version_preview_authority(
            events,
            conversation_id,
            workspace_version,
        )
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        return await _finished_preview_authority(
            runtime,
            events,
            conversation_id,
            selected_port,
        )
    return await _live_preview_authority(
        runtime,
        events,
        conversation_id,
        selected_port,
    )


async def _mint_canonical_preview(
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    request: Request,
    session: AuthSession,
    signer: PreviewCapabilitySigner,
    conversation_id: str,
    cid8: str,
    port: int,
    target: str,
    workspace_version: int | None,
    capture_generation: int | None,
) -> tuple[str, str, str, int | None]:
    authority_id = await _canonical_preview_authority(
        store,
        runtime,
        conversation_id,
        port,
        workspace_version=workspace_version,
    )
    if authority_id is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "preview_authority_unavailable"},
        )
    if _local_preview_request(request) and not disco_env("PREVIEW_ORIGIN_BASE", "").strip():
        now = int(time.time())
        lease = store.acquire_local_preview_lease(
            conversation_id=conversation_id,
            owner_id=session.owner_id,
            target_port=port,
            authority_id=authority_id,
            now=now,
            expires_at=now + preview_ttl_s(),
            listener_ports=local_preview_gateway_ports(),
        )
        if lease is None:
            raise HTTPException(
                status_code=503,
                detail={"reason": "local_preview_origin_pool_exhausted"},
            )
        bootstrap = _local_preview_bootstrap_url(
            request,
            lease.listener_port,
        )
    else:
        bootstrap = _preview_bootstrap_url(request, cid8, port)
    intent = signer.mint_intent(
        session=session,
        conversation_id=conversation_id,
        port=port,
        target_path=target,
        allow_websocket=True,
        http_methods=PREVIEW_APP_HTTP_METHODS,
        authority_id=authority_id,
        immutable_version=workspace_version,
        capture_generation=capture_generation,
    )
    return bootstrap, intent, authority_id, workspace_version


def _validate_capability_body(body: PreviewCapabilityBody) -> None:
    if (
        body.port is not None
        and body.port not in USER_PORTS
        and not (body.transport == "canonical" and is_managed_host_preview_port(body.port))
    ):
        raise HTTPException(
            status_code=404,
            detail={"reason": "unknown_port"},
        )
    if body.workspace_version is not None and body.transport != "canonical":
        raise HTTPException(
            status_code=400,
            detail={"reason": "workspace_version_requires_canonical_preview"},
        )


def _validated_target_path(
    target_path: str,
) -> tuple[str, urllib.parse.SplitResult, str]:
    target = target_path if target_path.startswith("/") else f"/{target_path}"
    target_parts = urllib.parse.urlsplit(target)
    if target_parts.scheme or target_parts.netloc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_preview_path"},
        )
    safe_target = _safe_capability_target_path(target_parts.path)
    if safe_target is None:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_preview_path"},
        )
    return target, target_parts, safe_target


async def _selected_capability_port(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    body: PreviewCapabilityBody,
) -> int:
    if body.transport not in {"path", "path_live", "canonical"}:
        return body.port if body.port is not None else PREVIEW_PORT
    selected_port = (
        _canonical_preview_port(runtime, conversation_id) if runtime is not None else PREVIEW_PORT
    )
    selected_port = await refresh_canonical_preview_port(
        store,
        runtime,
        conversation_id,
        selected_port,
        canonical=body.transport == "canonical",
        current=body.workspace_version is None,
    )
    if (
        selected_port is None
        and body.transport in {"path", "canonical"}
        and await _committed_static_capability_available(
            store,
            runtime,
            conversation_id,
            body.workspace_version,
        )
    ):
        selected_port = PREVIEW_PORT
    if selected_port is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "preview_unavailable"},
        )
    if body.port is not None and body.port != selected_port:
        raise HTTPException(
            status_code=400,
            detail={"reason": "path_preview_port_mismatch"},
        )
    return selected_port


def _host_preview_intent(
    signer: PreviewCapabilitySigner,
    session: AuthSession,
    request: Request,
    conversation_id: str,
    cid8: str,
    port: int,
    target: str,
    target_parts: urllib.parse.SplitResult,
    capture_generation: int | None,
) -> tuple[str, str]:
    reserved_host_target = (
        target_parts.path == PREVIEW_BOOTSTRAP_PATH
        or target_parts.path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/")
        or (
            target_parts.path.startswith("/conversations/") and "/preview-app/" in target_parts.path
        )
    )
    if reserved_host_target:
        raise HTTPException(
            status_code=400,
            detail={"reason": "reserved_preview_path"},
        )
    return (
        _preview_bootstrap_url(request, cid8, port),
        signer.mint_intent(
            session=session,
            conversation_id=conversation_id,
            port=port,
            target_path=target,
            allow_websocket=True,
            http_methods=PREVIEW_APP_HTTP_METHODS,
            capture_generation=capture_generation,
        ),
    )


def _path_preview_intent(
    signer: PreviewCapabilitySigner,
    session: AuthSession,
    request: Request,
    conversation_id: str,
    port: int,
    safe_target: str,
    target_parts: urllib.parse.SplitResult,
    *,
    allow_websocket: bool,
    capture_generation: int | None,
) -> tuple[str, str]:
    path_prefix = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{conversation_id}/"
    path_target = f"{path_prefix}{safe_target}"
    if target_parts.query:
        path_target = f"{path_target}?{target_parts.query}"
    if len(path_target) > MAX_PREVIEW_TARGET_PATH_CHARS:
        raise HTTPException(
            status_code=400,
            detail={"reason": "preview_target_too_long"},
        )
    return (
        _path_preview_bootstrap_url(request, conversation_id, port),
        signer.mint_intent(
            session=session,
            conversation_id=conversation_id,
            port=port,
            target_path=path_target,
            path_prefix=path_prefix,
            allow_websocket=allow_websocket,
            capture_generation=capture_generation,
        ),
    )


async def _capability_intent(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    signer: PreviewCapabilitySigner,
    request: Request,
    session: AuthSession,
    conversation_id: str,
    cid8: str,
    port: int,
    target: str,
    target_parts: urllib.parse.SplitResult,
    safe_target: str,
    body: PreviewCapabilityBody,
    capture_generation: int | None,
) -> tuple[str, str, str | None, int | None]:
    if body.transport == "canonical":
        return await _mint_canonical_preview(
            store=store,
            runtime=runtime,
            request=request,
            session=session,
            signer=signer,
            conversation_id=conversation_id,
            cid8=cid8,
            port=port,
            target=target,
            workspace_version=body.workspace_version,
            capture_generation=capture_generation,
        )
    if body.transport == "host":
        bootstrap, intent = _host_preview_intent(
            signer,
            session,
            request,
            conversation_id,
            cid8,
            port,
            target,
            target_parts,
            capture_generation,
        )
    else:
        bootstrap, intent = _path_preview_intent(
            signer,
            session,
            request,
            conversation_id,
            port,
            safe_target,
            target_parts,
            allow_websocket=body.transport == "path_live",
            capture_generation=capture_generation,
        )
    return bootstrap, intent, None, None


async def _preview_capability_response_unleased(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    signer: PreviewCapabilitySigner,
    conversation_id: str,
    body: PreviewCapabilityBody,
    request: Request,
    capture_generation: int | None = None,
) -> Response:
    _validate_capability_body(body)
    session = current_session(request)
    conversation_id = await require_owned_conversation(
        request,
        store,
        conversation_id,
    )
    cid8 = conversation_id.removeprefix("conv_")[:8]
    target, target_parts, safe_target = _validated_target_path(body.target_path)
    port = await _selected_capability_port(
        store,
        runtime,
        conversation_id,
        body,
    )
    (
        bootstrap,
        intent,
        authority_id,
        immutable_version,
    ) = await _capability_intent(
        store,
        runtime,
        signer,
        request,
        session,
        conversation_id,
        cid8,
        port,
        target,
        target_parts,
        safe_target,
        body,
        capture_generation,
    )
    return JSONResponse(
        {
            "bootstrap_url": bootstrap,
            "bootstrap_intent": intent,
            "target_path": target,
            "port": port,
            "transport": body.transport,
            "preview_authority": authority_id,
            "immutable_version": immutable_version,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _preview_capability_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    signer: PreviewCapabilitySigner,
    conversation_id: str,
    body: PreviewCapabilityBody,
    request: Request,
) -> Response:
    """Mint capability while retaining the finished preview's sandbox owner."""
    if runtime is None:
        return await _preview_capability_response_unleased(
            store,
            runtime,
            signer,
            conversation_id,
            body,
            request,
        )
    capture_lease = getattr(runtime.preview, "capture_lease", None)
    if not callable(capture_lease):
        # Narrow compatibility for route doubles that intentionally model only
        # capability selection; the composed runtime always supplies the lease.
        return await _preview_capability_response_unleased(
            store,
            runtime,
            signer,
            conversation_id,
            body,
            request,
        )
    begin_capture = getattr(runtime.preview, "begin_capture", None)
    capture_generation = None
    if callable(begin_capture):
        capture_generation = begin_capture(conversation_id)
    async with capture_lease(conversation_id):
        return await _preview_capability_response_unleased(
            store,
            runtime,
            signer,
            conversation_id,
            body,
            request,
            capture_generation,
        )


async def _redemption_body(request: Request) -> bytes | Response:
    if not preview_redemption_content_type(request.headers.get("content-type")):
        return Response(
            "invalid preview intent",
            status_code=403,
            media_type="text/plain",
        )
    if (
        SessionSigner().verify_cookie_header(cookie_header_from_headers(request.headers))
        is not None
    ):
        return Response("isolated preview session required", status_code=403)
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
                return Response("invalid preview intent", status_code=403)
        except ValueError:
            return Response("invalid preview intent", status_code=403)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
            return Response("invalid preview intent", status_code=403)
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class _RedeemedPathIntent:
    token: str
    target: str
    capability: object
    expected_prefix: str


def _request_label_port(request: Request) -> tuple[str, int]:
    request_label = (request.url.hostname or "").split(".", 1)[0].lower()
    try:
        port = int(request_label.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        port = 0
    return request_label, port


def _redeemed_path_intent(
    signer: PreviewCapabilitySigner,
    intent: str,
    cid8: str,
    request: Request,
) -> tuple[_RedeemedPathIntent | None, bool]:
    request_label, port = _request_label_port(request)
    if port not in USER_PORTS or port == NOVNC_PORT:
        return None, False
    redeemed = signer.redeem_intent(
        intent,
        cid8=cid8,
        port=port,
        path_scope="static",
        request_host_label=request_label,
    )
    if redeemed is None:
        return None, False
    token, target = redeemed
    target_path = urllib.parse.urlsplit(target).path
    capability = signer.verify(
        token,
        cid8=cid8,
        port=port,
        method="GET",
        path=target_path,
    )
    expected_prefix = (
        f"{ISOLATED_PATH_PREVIEW_PREFIX}/{capability.conversation_id}/"
        if capability is not None
        else ""
    )
    if capability is None or not target_path.startswith(expected_prefix):
        return None, True
    return (
        _RedeemedPathIntent(
            token=token,
            target=target,
            capability=capability,
            expected_prefix=expected_prefix,
        ),
        False,
    )


def _path_preview_navigation_response(
    request: Request,
    cid8: str,
    redeemed: _RedeemedPathIntent,
) -> Response:
    document, navigation_headers = preview_navigation_document(
        redeemed.target,
        clear_storage=False,
    )
    response = Response(
        document,
        status_code=200,
        headers=navigation_headers,
    )
    cookie_name = path_preview_cookie_name(cid8)
    if cross_site_iframe_headers(dict(request.headers)):
        response.headers.append(
            "set-cookie",
            f"{cookie_name}={redeemed.token}; Max-Age={preview_ttl_s()}; "
            f"Path={redeemed.expected_prefix}; HttpOnly; Secure; "
            "SameSite=None; Partitioned",
        )
    else:
        forwarded_scheme = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        response.set_cookie(
            cookie_name,
            redeemed.token,
            max_age=preview_ttl_s(),
            httponly=True,
            secure=(forwarded_scheme == "https" or request.url.scheme == "https"),
            samesite="strict",
            path=redeemed.expected_prefix,
        )
    return response


async def _path_preview_bootstrap_response(
    signer: PreviewCapabilitySigner,
    cid8: str,
    request: Request,
) -> Response:
    if len(cid8) != 8 or any(ch not in "0123456789abcdef" for ch in cid8.lower()):
        return Response(
            "invalid preview target",
            status_code=403,
            media_type="text/plain",
        )
    body = await _redemption_body(request)
    if isinstance(body, Response):
        return body
    parsed = parse_preview_redemption(body)
    if parsed is None:
        return Response(
            "invalid preview intent",
            status_code=403,
            media_type="text/plain",
        )
    redeemed, scope_mismatch = _redeemed_path_intent(
        signer,
        parsed,
        cid8,
        request,
    )
    if redeemed is None:
        return Response(
            ("preview intent scope mismatch" if scope_mismatch else "invalid preview intent"),
            status_code=403,
            media_type="text/plain",
        )
    return _path_preview_navigation_response(request, cid8, redeemed)

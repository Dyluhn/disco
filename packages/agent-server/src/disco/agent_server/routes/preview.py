"""Live-preview routes — availability, restart, and single-origin upstream proxies."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json as _json
import logging
import mimetypes
import re
import secrets
import urllib.parse
from pathlib import PurePosixPath
from typing import Literal

import httpx
import idna
import websockets
from disco.core import ConversationStatus, DeliverableEvent, StatusEvent, WorkspaceVersionEvent
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    MAX_PREVIEW_TARGET_PATH_CHARS,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_BOOTSTRAP_PATH,
    PreviewCapabilitySigner,
    SessionSigner,
    cookie_header_from_headers,
    origin_matches_request_host,
    path_preview_cookie_name,
    path_preview_host_label,
    preview_ttl_s,
)
from disco.core.env import disco_env
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.base import strip_redundant_workspace_prefix
from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import current_session, websocket_session
from ..preview_bootstrap import (
    MAX_PREVIEW_REDEMPTION_BODY_BYTES,
    cross_site_iframe_headers,
    parse_preview_redemption,
    preview_navigation_document,
    preview_redemption_content_type,
)
from ..preview_inject import inject_element_mention_picker, inject_selection_agent
from ..runtime import ConversationRuntime
from ._common import (
    require_owned_conversation,
    require_owned_conversation_for_owner,
)

_LOG = logging.getLogger(__name__)


class PreviewCapabilityBody(BaseModel):
    # Path-preview callers omit the port: PreviewManager selects it. An explicit
    # value is accepted only when it exactly matches that server selection.
    port: int | None = None
    target_path: str = Field("/", max_length=MAX_PREVIEW_TARGET_PATH_CHARS)
    transport: Literal["host", "path", "path_live"] = "host"


_ENCODED_PREVIEW_STRUCTURAL = re.compile(r"%([0-9a-fA-F]{2})")


def _safe_capability_target_path(raw: str) -> str | None:
    """Reject URL encodings browsers may reinterpret as path structure."""

    matched_escapes = set(_ENCODED_PREVIEW_STRUCTURAL.finditer(raw))
    for match in matched_escapes:
        if chr(int(match.group(1), 16)) in {".", "/", "\\", "\x00", "%"}:
            return None
    # A stray '%' is browser-dependent and cannot be signed canonically.
    without_escapes = _ENCODED_PREVIEW_STRUCTURAL.sub("", raw)
    if "%" in without_escapes:
        return None
    return _safe_preview_path(raw, allow_leading_slash=True)


def _safe_preview_path(raw: str, *, allow_leading_slash: bool) -> str | None:
    """Normalize one URL/workspace path without ever decoding it a second time.

    Starlette has already percent-decoded the route parameter.  A second unquote
    here would turn a harmless literal ``%2e%2e`` filename into traversal.  Empty
    and redundant slash components are allowed so ``preview-app//assets/x`` and
    ``preview-app/assets/x`` address the same file; dot-dot, backslashes, NULs,
    and an absolute manifest entry fail closed.
    """
    value = raw.strip()
    if "\x00" in value or "\\" in value:
        return None
    if not allow_leading_slash and value.startswith("/"):
        return None
    parts = [part for part in PurePosixPath(value.strip("/")).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _snapshot_request_path(ws, requested_path: str, entry_path: str | None):  # noqa: ANN001
    """Resolve a preview URL against the selected deliverable's directory.

    A completed app is a graph rooted beside its declared HTML entry.  Browser
    requests such as ``assets/app.js`` therefore resolve beside
    ``dist/index.html`` rather than at the workspace root.  Returning ``None``
    means the selected entry itself is unsafe or absent; callers must not then
    fall back to a stale root index.
    """
    requested = _safe_preview_path(requested_path, allow_leading_slash=True)
    if requested is None:
        return None

    if entry_path is None:
        rel = requested or "index.html"
        return (ws / rel).resolve()

    normalized_entry = strip_redundant_workspace_prefix(entry_path.strip())
    entry = _safe_preview_path(normalized_entry, allow_leading_slash=False)
    if entry is None:
        return None
    if entry in {"", "."}:
        entry = "index.html"

    selected = (ws / entry).resolve()
    if not selected.is_relative_to(ws):
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
    # Accept both the canonical route-relative asset path and an already-prefixed
    # workspace path.  This makes links authored as either ``assets/x`` or
    # ``dist/assets/x`` converge without ever accepting traversal.
    if base_text and (requested == base_text or requested.startswith(f"{base_text}/")):
        rel = requested
    else:
        rel = f"{base_text}/{requested}" if base_text else requested
    return (ws / rel).resolve()


def _selected_app_entry(events: list, *, version: int | None) -> str | None:  # noqa: ANN001
    selected: str | None = None
    for event in events:
        if isinstance(event, DeliverableEvent) and event.artifact_kind == "app":
            selected = event.path
        if (
            version is not None
            and isinstance(event, WorkspaceVersionEvent)
            and event.version_seq == version
        ):
            return selected
    return selected if version is None else None


def _finished_snapshot_is_committed(events: list) -> bool:  # noqa: ANN001
    latest_status = next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)
    if latest_status is None or latest_status.status != ConversationStatus.FINISHED:
        return False
    status_seq = latest_status.seq or -1
    return any(
        isinstance(event, WorkspaceVersionEvent) and (event.seq or -1) > status_seq
        for event in events
    )


def _preview_websocket_origin_allowed(
    origin: str | None,
    host: str | None,
    *,
    websocket_scheme: str,
    forwarded_proto: str | None = None,
) -> bool:
    forwarded_proto = (forwarded_proto or "").split(",", 1)[0].strip().lower()
    websocket_scheme = websocket_scheme.lower()
    expected_origin_scheme = {"ws": "http", "wss": "https"}.get(websocket_scheme, websocket_scheme)
    if forwarded_proto in {"http", "https"}:
        expected_origin_scheme = forwarded_proto
    try:
        origin_scheme = urllib.parse.urlsplit(origin or "").scheme.lower()
    except ValueError:
        origin_scheme = ""
    return origin_matches_request_host(origin, host) and origin_scheme == expected_origin_scheme


async def _path_preview_websocket_capability_owner(
    websocket: WebSocket,
    store: SqliteEventStore,
    conversation_id: str,
) -> tuple[str | None, str, int | None]:
    """Authenticate an isolated path-live HMR socket without an app session."""

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
    cap = PreviewCapabilitySigner().verify_cookie_header(
        cookie_header_from_headers(websocket.headers),
        cookie_name,
        cid8=cid8,
        port=port,
        method="WEBSOCKET",
        path=websocket.url.path,
    )
    if cap is None or cap.conversation_id != conversation_id:
        return None, "preview capability required", None
    owner_id = await store.conversation_owner_id(conversation_id)
    if owner_id is None:
        return None, "conversation not found", None
    if owner_id != cap.owner_id:
        return None, "conversation forbidden", None
    return owner_id, "", port


def _forwarded_preview_query(request: Request) -> str:
    pairs = urllib.parse.parse_qsl(request.url.query, keep_blank_values=True)
    # ``version`` belongs to the host snapshot selector and must never leak to a
    # user's dev server.  All other query fields retain their semantic values.
    return urllib.parse.urlencode(
        [(key, value) for key, value in pairs if key != "version"], doseq=True
    )


def _serve_static_from_snapshot(
    runtime: ConversationRuntime,
    conversation_id: str,
    rel_path: str,
    *,
    version: int | None = None,
    entry_path: str | None = None,
    inject_selection: bool = False,
) -> Response | None:
    """runthru-v2: serve a FINISHED build's static site DIRECTLY from the host
    ProjectStore snapshot when the sandbox can't be woken (build finished + reaped,
    or the configured backend is unavailable). The built files already sit on disk
    at projects/{cid}/workspace/ — returning a 503 for a file we HAVE is the bug the
    user hit ("preview not available" on a completed app). Jailed to the snapshot
    workspace (mirrors files.py), normalizes a redundant 'workspace/' prefix."""
    if is_runtime_secret_path(rel_path):
        return None
    try:
        ps = runtime.project_store()
        if ps is None or ps.status() != StorageStatus.OK:
            return None
        if version is None:
            ws = ps.path_for(conversation_id).resolve()
        else:
            ws = ps.version_workspace_path(conversation_id, version).resolve()
    except StorageError:
        if version is not None:
            return Response("version not found", status_code=404, media_type="text/plain")
        return None
    except Exception:  # noqa: BLE001 — no snapshot → caller falls back to 503
        return None
    target = _snapshot_request_path(ws, rel_path, entry_path)
    if target is None:
        if entry_path is not None:
            return Response("preview target not found", status_code=404, media_type="text/plain")
        return None
    if target.is_dir():
        target = (target / "index.html").resolve()
    if (
        not target.is_relative_to(ws)
        or is_runtime_secret_path(target.relative_to(ws).as_posix())
        or not target.is_file()
    ):
        if entry_path is not None:
            return Response("preview asset not found", status_code=404, media_type="text/plain")
        return None
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    body = target.read_bytes()
    if inject_selection:
        body = inject_selection_agent(body, ctype)
    body = inject_element_mention_picker(body, ctype)
    return Response(content=body, media_type=ctype)


async def _fetch_inside_response(
    runtime: ConversationRuntime, conversation_id: str, port: int, rel_path: str
) -> Response | None:
    """Fix 2 (B-E): when no host port is published (sealed/filtered backend), reach
    the agent's dev server through a liveness proxy that curls it from INSIDE the
    sandbox. Returns a Response only when the in-sandbox server actually answers;
    None lets the caller fall through to the snapshot/503 path (honest unavailable)."""
    # SECURITY (noVNC gate-bypass fix): NOVNC_PORT is in USER_PORTS, so a request for
    # 6080 reaches here when wake_for_preview → None. But for 6080 that None is the
    # live-browser GATE (port_upstream refuses NOVNC_PORT when the feature is disabled),
    # NOT merely "no host port published". curling the stale noVNC HTTP surface from
    # inside the box would re-expose a disabled live-browser surface — bypassing the
    # gate. The noVNC surface is served EXCLUSIVELY through the gated published-port
    # proxy; the exec-curl fallback is for genuine dev-server/preview ports only.
    if port == NOVNC_PORT:
        return None
    try:
        session = runtime.live_session(conversation_id)
        if session is None:
            return None
        got = await session.fetch_inside(port, rel_path)
    except Exception:  # noqa: BLE001 — a liveness probe must never 500 the preview route
        return None
    if got is None:
        return None
    status, body, ctype = got
    media_type = ctype or "application/octet-stream"
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=status,
        media_type=media_type,
    )


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


def _site_boundary_key(hostname: str) -> tuple[str, ...]:
    """Conservative suffix key used to reject a same-site preview override.

    Sharing the final two DNS labels is sufficient evidence that two hosts may
    share a registrable parent. It intentionally rejects some safe multi-label
    public-suffix deployments rather than accepting an uncertain cookie boundary.
    """

    labels = tuple(part for part in hostname.lower().rstrip(".").split(".") if part)
    return labels[-2:] if len(labels) >= 2 else labels


_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _canonical_dns_hostname(hostname: str) -> str:
    """Return one browser-equivalent ASCII DNS name, or an empty string.

    Browsers canonicalize Unicode hostnames to IDNA before applying cookie-site
    boundaries.  Security comparisons must therefore use the same ASCII form;
    comparing a Unicode spelling directly with its punycode alias can mistake
    the same registrable site for two isolated sites.
    """

    try:
        canonical = idna.encode(
            hostname.rstrip("."),
            uts46=True,
            transitional=False,
            std3_rules=True,
        ).decode("ascii")
    except idna.IDNAError:
        return ""
    labels = canonical.split(".")
    if (
        not canonical
        or len(canonical.encode("ascii")) > 253
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
    ):
        return ""
    return canonical


def _preview_origin_base(request: Request) -> tuple[str, str, int | None]:
    """Resolve the wildcard preview base, preferring an isolated operator site."""

    configured = disco_env("PREVIEW_ORIGIN_BASE", "").strip()
    if configured:
        try:
            parsed = urllib.parse.urlsplit(configured)
            configured_port = parsed.port
        except ValueError as exc:
            raise HTTPException(
                status_code=500, detail={"reason": "preview_origin_base_invalid"}
            ) from exc
        hostname = _canonical_dns_hostname(parsed.hostname or "")
        hostname_labels = hostname.split(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or len(hostname_labels) < 2
            or configured_port == 0
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise HTTPException(status_code=500, detail={"reason": "preview_origin_base_invalid"})
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise HTTPException(status_code=500, detail={"reason": "preview_origin_base_invalid"})
        request_hostname = _canonical_dns_hostname(request.url.hostname or "localhost")
        public_ui = disco_env("PUBLIC_UI_URL", "").strip()
        try:
            public_ui_url = urllib.parse.urlsplit(public_ui)
            public_ui_hostname = _canonical_dns_hostname(public_ui_url.hostname or "")
        except ValueError:
            public_ui_hostname = ""
            public_ui_url = urllib.parse.SplitResult("", "", "", "", "")
        if public_ui_url.scheme not in {"http", "https"} or not public_ui_hostname:
            raise HTTPException(
                status_code=500,
                detail={"reason": "preview_origin_base_public_ui_required"},
            )
        privileged_hosts = (request_hostname, public_ui_hostname)
        if any(
            privileged and _site_boundary_key(hostname) == _site_boundary_key(privileged)
            for privileged in privileged_hosts
        ):
            raise HTTPException(
                status_code=500, detail={"reason": "preview_origin_base_not_isolated"}
            )
        return parsed.scheme, hostname, configured_port

    url = request.url
    hostname = (url.hostname or "localhost").lower().rstrip(".")
    if hostname in {"127.0.0.1", "::1", "localhost"}:
        base_hostname = "localhost"
    else:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            base_hostname = _canonical_dns_hostname(hostname)
            if not base_hostname:
                raise HTTPException(
                    status_code=409, detail={"reason": "preview_wildcard_dns_required"}
                ) from None
        else:
            # A label prefixed to a bare LAN/tailnet IP is not DNS. Returning
            # that URL would consume a one-time intent into an unreachable host.
            raise HTTPException(status_code=409, detail={"reason": "preview_wildcard_dns_required"})
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded if forwarded in {"http", "https"} else url.scheme
    return scheme, base_hostname, url.port


def _preview_netloc(label: str, base_hostname: str, base_port: int | None) -> str:
    preview_host = f"{label}.{base_hostname}"
    if len(preview_host.encode("ascii")) > 253:
        raise HTTPException(status_code=500, detail={"reason": "preview_origin_host_too_long"})
    return preview_host if base_port is None else f"{preview_host}:{base_port}"


def _preview_bootstrap_url(request: Request, cid8: str, port: int) -> str:
    scheme, base_hostname, base_port = _preview_origin_base(request)
    netloc = _preview_netloc(f"p2-{cid8}-{port}", base_hostname, base_port)
    return urllib.parse.urlunparse((scheme, netloc, PREVIEW_BOOTSTRAP_PATH, "", "", ""))


def _path_preview_bootstrap_url(request: Request, conversation_id: str, port: int) -> str:
    """Return a Firefox-safe, per-CID origin for path/static previews."""
    cid8 = conversation_id.removeprefix("conv_")[:8]
    scheme, base_hostname, base_port = _preview_origin_base(request)
    # Keep static/path content off the stable p2 live-host origin. The fresh p3s
    # namespace is a migration boundary for old workers/caches and gives every
    # conversation its own browser storage origin without destructive clearing.
    netloc = _preview_netloc(
        path_preview_host_label(conversation_id, port), base_hostname, base_port
    )
    path = f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}"
    return urllib.parse.urlunparse((scheme, netloc, path, "", "", ""))


def _path_preview_port_from_label(conversation_id: str, request_label: str) -> int | None:
    """Recover only an exact curated port from a full-CID-bound p3s host label."""
    for port in sorted(USER_PORTS - {NOVNC_PORT}):
        try:
            expected = path_preview_host_label(conversation_id, port)
        except ValueError:
            return None
        if secrets.compare_digest(expected, request_label.strip().lower()):
            return port
    return None


async def _wake_for_preview(
    runtime: ConversationRuntime, cid8: str, port: int, *, owner_id: str
) -> str | None:
    try:
        return await runtime.wake_for_preview(cid8, port, owner_id=owner_id)
    except TypeError:
        return await runtime.wake_for_preview(cid8, port)  # type: ignore[call-arg]


def _canonical_preview_port(runtime: ConversationRuntime, conversation_id: str) -> int | None:
    """Use the active PreviewManager target without weakening origin capabilities."""
    resolver = getattr(runtime, "preview_target_port", None)
    if resolver is None:
        return PREVIEW_PORT
    try:
        port = resolver(conversation_id)
    except Exception:  # noqa: BLE001 — corrupt selection cannot choose an upstream
        return None
    if port is None:
        return None
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or port not in USER_PORTS
        or port == NOVNC_PORT
    ):
        return None
    return port


async def _committed_static_capability_available(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    target_parts: urllib.parse.SplitResult,
) -> bool:
    """Narrow legacy-8000 namespace for a proven immutable app snapshot only."""
    if runtime is None:
        return False
    versions = urllib.parse.parse_qs(target_parts.query).get("version", [])
    version: int | None = None
    if versions:
        if len(versions) != 1:
            return False
        try:
            version = int(versions[0])
        except ValueError:
            return False
        if version < 1:
            return False
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing evidence cannot authorize fallback
        return False
    if version is None and not _finished_snapshot_is_committed(events):
        return False
    entry_path = _selected_app_entry(events, version=version)
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


async def _proxy_websocket_to_upstream(websocket: WebSocket, upstream: str, rel_path: str) -> None:
    """Bridge preview WebSocket frames to the live upstream (Vite HMR, etc.)."""
    upstream_parsed = urllib.parse.urlparse(upstream)
    ws_scheme = "wss" if upstream_parsed.scheme in {"https", "wss"} else "ws"
    target_path = "/" + rel_path.lstrip("/")
    query_string = websocket.scope.get("query_string", b"").decode("latin1")
    target_url = urllib.parse.urlunparse(
        (ws_scheme, upstream_parsed.netloc, target_path, "", query_string, "")
    )

    from websockets.typing import Subprotocol

    subprotocols = [Subprotocol(p) for p in websocket.scope.get("subprotocols", [])]
    if not subprotocols:
        proto = websocket.headers.get("sec-websocket-protocol")
        if proto:
            subprotocols = [Subprotocol(p.strip()) for p in proto.split(",") if p.strip()]

    try:
        if subprotocols:
            ws_client = await websockets.connect(target_url, subprotocols=subprotocols)
        else:
            # websockets 16 turns [] into an invalid empty protocol header.
            ws_client = await websockets.connect(target_url)
    except Exception as exc:  # noqa: BLE001 — failed upgrade should close, not 500
        _LOG.warning("preview websocket upstream connect error: %s", exc)
        await _close_ws(websocket, 1011, "preview upstream unreachable")
        return

    await websocket.accept(subprotocol=ws_client.subprotocol)

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
        except Exception:  # noqa: BLE001 — peer went away / upstream closed
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
        except Exception:  # noqa: BLE001 — downstream disconnected / send failed
            await _close_ws(websocket, 1011, "preview websocket failed")

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())
    try:
        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        with contextlib.suppress(Exception):
            await ws_client.close()


def _register_preview_capability_route(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    cap_signer = PreviewCapabilitySigner(redemption_store=store)

    @router.post("/conversations/{conversation_id}/preview/capability")
    async def preview_capability(
        conversation_id: str,
        body: PreviewCapabilityBody,
        request: Request,
    ) -> Response:
        if body.port is not None and body.port not in USER_PORTS:
            raise HTTPException(status_code=404, detail={"reason": "unknown_port"})
        session = current_session(request)
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        cid8 = conversation_id.removeprefix("conv_")[:8]
        target = body.target_path if body.target_path.startswith("/") else f"/{body.target_path}"
        target_parts = urllib.parse.urlsplit(target)
        if target_parts.scheme or target_parts.netloc or target_parts.fragment:
            raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
        safe_target = _safe_capability_target_path(target_parts.path)
        if safe_target is None:
            raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
        if body.transport in {"path", "path_live"}:
            selected_port = (
                _canonical_preview_port(runtime, conversation_id)
                if runtime is not None
                else PREVIEW_PORT
            )
            if (
                selected_port is None
                and body.transport == "path"
                and await _committed_static_capability_available(
                    store, runtime, conversation_id, target_parts
                )
            ):
                selected_port = PREVIEW_PORT
            if selected_port is None:
                raise HTTPException(status_code=409, detail={"reason": "preview_unavailable"})
            if body.port is not None and body.port != selected_port:
                raise HTTPException(
                    status_code=400, detail={"reason": "path_preview_port_mismatch"}
                )
            port = selected_port
        else:
            port = body.port if body.port is not None else PREVIEW_PORT

        if body.transport == "host":
            reserved_host_target = (
                target_parts.path == PREVIEW_BOOTSTRAP_PATH
                or target_parts.path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/")
                or (
                    target_parts.path.startswith("/conversations/")
                    and "/preview-app/" in target_parts.path
                )
            )
            if reserved_host_target:
                raise HTTPException(status_code=400, detail={"reason": "reserved_preview_path"})
            # Construct/validate the isolated origin before registering a JTI.
            # Invalid bare-IP deployments therefore leave no unusable intent.
            bootstrap = _preview_bootstrap_url(request, cid8, port)
            intent = cap_signer.mint_intent(
                session=session,
                conversation_id=conversation_id,
                port=port,
                target_path=target,
                allow_websocket=True,
                http_methods=PREVIEW_APP_HTTP_METHODS,
            )
        else:
            path_prefix = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{conversation_id}/"
            path_target = f"{path_prefix}{safe_target}"
            if target_parts.query:
                path_target = f"{path_target}?{target_parts.query}"
            if len(path_target) > MAX_PREVIEW_TARGET_PATH_CHARS:
                raise HTTPException(status_code=400, detail={"reason": "preview_target_too_long"})
            bootstrap = _path_preview_bootstrap_url(request, conversation_id, port)
            intent = cap_signer.mint_intent(
                session=session,
                conversation_id=conversation_id,
                port=port,
                target_path=path_target,
                path_prefix=path_prefix,
                allow_websocket=body.transport == "path_live",
            )

        return JSONResponse(
            {
                "bootstrap_url": bootstrap,
                "bootstrap_intent": intent,
                "target_path": target,
                "port": port,
                "transport": body.transport,
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @router.post(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{{cid8}}")
    async def path_preview_bootstrap(cid8: str, request: Request) -> Response:
        if len(cid8) != 8 or any(ch not in "0123456789abcdef" for ch in cid8.lower()):
            return Response("invalid preview target", status_code=403, media_type="text/plain")
        if not preview_redemption_content_type(request.headers.get("content-type")):
            return Response("invalid preview intent", status_code=403, media_type="text/plain")
        if (
            SessionSigner().verify_cookie_header(cookie_header_from_headers(request.headers))
            is not None
        ):
            # A generated-content origin must never transition from a full app
            # session into preview mode. Use the other isolated loopback host or
            # the versioned remote wildcard instead.
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
        parsed = parse_preview_redemption(b"".join(chunks))
        if parsed is None:
            return Response("invalid preview intent", status_code=403, media_type="text/plain")
        intent = parsed
        request_label = (request.url.hostname or "").split(".", 1)[0].lower()
        try:
            port = int(request_label.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            port = 0
        if port not in USER_PORTS or port == NOVNC_PORT:
            return Response("invalid preview intent", status_code=403, media_type="text/plain")
        # redeem_intent decodes the signed full conversation id and compares the
        # complete p3s label, so a forged digest/port suffix cannot consume the JTI.
        redeemed = cap_signer.redeem_intent(
            intent,
            cid8=cid8,
            port=port,
            path_scope="static",
            request_host_label=request_label,
        )
        if redeemed is None:
            return Response("invalid preview intent", status_code=403, media_type="text/plain")
        token, target = redeemed
        target_path = target.split("?", 1)[0]
        cap = cap_signer.verify(
            token,
            cid8=cid8,
            port=port,
            method="GET",
            path=target_path,
        )
        expected_prefix = (
            f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cap.conversation_id}/" if cap is not None else ""
        )
        if cap is None or not target_path.startswith(expected_prefix):
            return Response(
                "preview intent scope mismatch", status_code=403, media_type="text/plain"
            )
        document, navigation_headers = preview_navigation_document(target, clear_storage=False)
        response = Response(document, status_code=200, headers=navigation_headers)
        cookie_name = path_preview_cookie_name(cid8)
        if cross_site_iframe_headers(dict(request.headers)):
            # H086/H110: this is the actual form-navigation request, so its
            # browser-generated fetch metadata truthfully selects iframe posture.
            response.headers.append(
                "set-cookie",
                f"{cookie_name}={token}; Max-Age={preview_ttl_s()}; "
                f"Path={expected_prefix}; HttpOnly; Secure; SameSite=None; Partitioned",
            )
        else:
            forwarded_scheme = (
                (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            )
            response.set_cookie(
                cookie_name,
                token,
                max_age=preview_ttl_s(),
                httponly=True,
                secure=forwarded_scheme == "https" or request.url.scheme == "https",
                samesite="strict",
                path=expected_prefix,
            )
        return response


def _register_live_browser_start_route(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    @router.post("/conversations/{conversation_id}/browser/live-url")
    async def browser_live_url(conversation_id: str, request: Request) -> Response:
        """Lazily start the noVNC live-view stack and return the gated proxy port."""
        if runtime is None:
            return Response("no runtime", status_code=503, media_type="text/plain")
        owner_id = current_session(request).owner_id
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        try:
            cfg = runtime._config_store.load()
            if not cfg.live_browser.enabled:
                return Response(
                    _json.dumps(
                        {
                            "reason": "disabled",
                            "message": "Live browser is off — enable it in Settings → Agent.",
                        }
                    ),
                    status_code=503,
                    media_type="application/json",
                )
        except Exception:
            return Response("config unavailable", status_code=503, media_type="text/plain")

        cid8 = conversation_id.removeprefix("conv_")[:8]
        session = runtime.live_session(conversation_id)
        if session is None:
            return Response(
                _json.dumps(
                    {
                        "reason": "no_sandbox",
                        "message": (
                            "No sandbox running for this conversation — start the agent first."
                        ),
                    }
                ),
                status_code=503,
                media_type="application/json",
            )
        if not getattr(session, "supports_live_view", False):
            return Response(
                _json.dumps(
                    {
                        "reason": "unsupported_backend",
                        "message": (
                            "Live browser needs the gVisor sandbox — this backend can't run it."
                        ),
                    }
                ),
                status_code=503,
                media_type="application/json",
            )

        try:
            res = await session.exec_shell("curl -sf http://127.0.0.1:8901/health", timeout_s=3)
            if res.exit_code != 0:
                return Response(
                    _json.dumps(
                        {
                            "reason": "no_daemon",
                            "message": "Browser daemon not running — use the browser tool first.",
                        }
                    ),
                    status_code=503,
                    media_type="application/json",
                )
            job_escaped = _json.dumps({"action": "live_start"}).replace("'", "'\"'\"'")
            res2 = await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json' -d '{job_escaped}'",
                timeout_s=30,
            )
            if res2.exit_code != 0:
                return Response(
                    _json.dumps(
                        {
                            "reason": "live_start_failed",
                            "message": "Failed to start live view stack.",
                        }
                    ),
                    status_code=503,
                    media_type="application/json",
                )
            data = _json.loads(res2.stdout)
            if not data.get("ok"):
                msg = data.get("error", "live_start failed")
                return Response(
                    _json.dumps({"reason": "live_start_failed", "message": msg}),
                    status_code=503,
                    media_type="application/json",
                )
        except Exception as e:  # noqa: BLE001
            return Response(
                _json.dumps({"reason": "error", "message": str(e)[:120]}),
                status_code=503,
                media_type="application/json",
            )

        upstream = await _wake_for_preview(runtime, cid8, NOVNC_PORT, owner_id=owner_id)
        if upstream is None:
            return Response(
                _json.dumps(
                    {
                        "reason": "no_upstream",
                        "message": "noVNC port not yet exposed by the sandbox.",
                    }
                ),
                status_code=503,
                media_type="application/json",
            )
        return JSONResponse(
            {
                "ready": True,
                "novnc_path": "/vnc.html?autoconnect=1&view_only=1",
                "port": NOVNC_PORT,
            }
        )


def _register_live_browser_status_routes(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    @router.get("/conversations/{conversation_id}/browser/live-ready")
    async def browser_live_ready(conversation_id: str, request: Request) -> Response:
        """Side-effect-free readiness probe for the Live button."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return JSONResponse({"ready": False, "reason": "no_runtime"})
        try:
            cfg = runtime._config_store.load()
            if not cfg.live_browser.enabled:
                return JSONResponse({"ready": False, "reason": "disabled"})
        except Exception:  # noqa: BLE001
            return JSONResponse({"ready": False, "reason": "config_unavailable"})
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ready": False, "reason": "no_sandbox"})
        if not getattr(session, "supports_live_view", False):
            return JSONResponse({"ready": False, "reason": "unsupported_backend"})
        try:
            res = await session.exec_shell("curl -sf http://127.0.0.1:8901/health", timeout_s=3)
        except Exception:  # noqa: BLE001
            return JSONResponse({"ready": False, "reason": "no_daemon"})
        if res.exit_code != 0:
            return JSONResponse({"ready": False, "reason": "no_daemon"})
        return JSONResponse({"ready": True, "reason": "ready"})

    @router.post("/conversations/{conversation_id}/browser/live-touch")
    async def browser_live_touch(conversation_id: str, request: Request) -> Response:
        """Heartbeat from the open Live pane; best-effort and always 200."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return JSONResponse({"ok": True, "note": "no runtime"})
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ok": True, "note": "no sandbox"})
        try:
            job_escaped = _json.dumps({"action": "live_touch"}).replace("'", "'\"'\"'")
            await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json' -d '{job_escaped}'",
                timeout_s=5,
            )
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": True, "note": "touch best-effort"})
        return JSONResponse({"ok": True})

    @router.post("/conversations/{conversation_id}/browser/live-stop")
    async def browser_live_stop(conversation_id: str, request: Request) -> Response:
        """Tear the live-view stack down inside the sandbox; best-effort."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return JSONResponse({"ok": True, "note": "no runtime"})
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ok": True, "note": "no sandbox"})
        try:
            job_escaped = _json.dumps({"action": "live_stop"}).replace("'", "'\"'\"'")
            await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json' -d '{job_escaped}'",
                timeout_s=10,
            )
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": True, "note": "teardown best-effort"})
        return JSONResponse({"ok": True})


def _register_port_preview_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    """Register the curated per-port HTTP and WebSocket proxies."""

    @router.get("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.get("/conversations/{conversation_id}/port/{port}/")
    async def port_app(
        request: Request, conversation_id: str, port: int, path: str = ""
    ) -> Response:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if port not in USER_PORTS:
            return Response("unknown port", status_code=404, media_type="text/plain")
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        owner_id = current_session(request).owner_id
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await _wake_for_preview(runtime, cid8, port, owner_id=owner_id)
        if upstream is None:
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
            if served is not None:
                return served
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream may not be ready
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
            if served is not None:
                return served
            return Response(
                "preview upstream unreachable", status_code=502, media_type="text/plain"
            )
        media_type = response.headers.get("content-type", "text/html")
        return Response(
            content=inject_element_mention_picker(response.content, media_type),
            status_code=response.status_code,
            media_type=media_type,
        )

    @router.websocket("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.websocket("/conversations/{conversation_id}/port/{port}/")
    async def port_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        port: int,
        path: str = "",
    ) -> None:
        session = websocket_session(websocket)
        if session is None:
            await _close_ws(websocket, 1008, "auth required")
            return
        try:
            conversation_id = await require_owned_conversation_for_owner(
                store,
                conversation_id,
                session.owner_id,
                owner_bypass=session.session_id == "test-session",
            )
        except HTTPException:
            await _close_ws(websocket, 1008, "conversation forbidden")
            return
        if port not in USER_PORTS:
            await _close_ws(websocket, 1008, "unknown port")
            return
        if runtime is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await _wake_for_preview(runtime, cid8, port, owner_id=session.owner_id)
        if upstream is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)


def make_preview_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()
    _register_preview_capability_route(router, store, runtime)
    _register_live_browser_start_route(router, store, runtime)
    _register_live_browser_status_routes(router, store, runtime)
    _register_port_preview_routes(router, store, runtime)

    @router.get("/conversations/{conversation_id}/preview")
    async def get_preview(conversation_id: str, request: Request) -> dict:
        """Backend-aware live preview availability (the browser iframes the proxy below)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"available": False, "reason": "no runtime"}
        return await runtime.preview(conversation_id)

    @router.post("/conversations/{conversation_id}/preview/restart")
    async def ensure_preview(conversation_id: str, request: Request) -> dict:
        """Bring a down preview back on demand (§E7) — the UI 'Restart preview' button.
        Bounded + safe (same path as SandboxSession.ensure_preview)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"ok": False}
        return {"ok": await runtime.ensure_preview(conversation_id)}

    @router.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.get("/conversations/{conversation_id}/preview-app/")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/{{path:path}}")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages.
    # WALK-10: now wakes suspended sandboxes via wake_for_preview — fixes the Open button
    # and the PreviewPane "Open in new tab" link that returned 503 after sandbox auto-suspend.
    async def preview_app(
        request: Request,
        conversation_id: str,
        path: str = "",
        version: int | None = Query(default=None),
    ) -> Response:
        """Proxy the agent's dev server through THIS (tailnet-reachable) origin — the
        backend-derived upstream (localhost for local, the remote tailnet IP for gVisor) is
        reached server-side, so no random container port is exposed and previews work over
        the tailnet. Forwards GET; good for a built page (single-origin assets).

        Uses wake_for_preview so a suspended sandbox is rematerialised on demand —
        the passive preview_upstream check only finds live in-memory executors."""
        preview_cap = getattr(request.state, "preview_capability", None)
        if preview_cap is None:
            conversation_id = await require_owned_conversation(request, store, conversation_id)
            owner_id = current_session(request).owner_id
        else:
            if preview_cap.conversation_id != conversation_id:
                return Response(
                    "preview capability mismatch", status_code=403, media_type="text/plain"
                )
            owner_id = preview_cap.owner_id
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        cid8 = conversation_id.removeprefix("conv_")[:8]
        safe_path = _safe_preview_path(path, allow_leading_slash=True)
        if safe_path is None or is_runtime_secret_path(safe_path):
            return Response("preview path not found", status_code=404, media_type="text/plain")
        try:
            events = await store.get_events(conversation_id)
        except Exception:  # noqa: BLE001 — selected-target metadata is security relevant
            _LOG.warning(
                "preview target metadata unavailable for %s", conversation_id, exc_info=True
            )
            return Response(
                "preview metadata unavailable", status_code=503, media_type="text/plain"
            )
        entry_path = _selected_app_entry(events, version=version)
        # Historical preview is static-only AND must NEVER fall through to the live
        # proxy: a ?version request answered by the live sandbox would show current
        # bytes under a "viewing vN" banner — a false affordance. Version requests
        # serve from the version snapshot or 404, full stop.
        if version is not None:
            served = _serve_static_from_snapshot(
                runtime,
                conversation_id,
                safe_path,
                version=version,
                entry_path=entry_path,
                inject_selection=preview_cap is not None,
            )
            if served is not None:
                return served
            if entry_path is not None:
                return Response(
                    "committed preview unavailable", status_code=503, media_type="text/plain"
                )
            return Response("version not found", status_code=404, media_type="text/plain")
        # Once the FINISHED workspace has a durable commit marker, its selected
        # deliverable is authoritative.  Serving it before waking/proxying avoids
        # a still-running stale scaffold at `/` winning over the completed app.
        if _finished_snapshot_is_committed(events):
            served = _serve_static_from_snapshot(
                runtime,
                conversation_id,
                safe_path,
                entry_path=entry_path,
                inject_selection=preview_cap is not None,
            )
            if served is not None:
                return served
            if entry_path is not None:
                return Response(
                    "committed preview unavailable", status_code=503, media_type="text/plain"
                )
        target_port = (
            preview_cap.port
            if preview_cap is not None
            else _canonical_preview_port(runtime, conversation_id)
        )
        upstream = (
            await _wake_for_preview(runtime, cid8, target_port, owner_id=owner_id)
            if target_port is not None
            else None
        )
        query = _forwarded_preview_query(request)
        upstream_path = f"{safe_path}?{query}" if query else safe_path
        if upstream is None:
            # Fix 2 (B-E): on sealed/filtered boxes no host port is published, so
            # `wake_for_preview` resolves None even with a live dev server. Before
            # falling back to the (stale mid-run) snapshot, try a liveness proxy that
            # curls the server from INSIDE the sandbox — genuinely liveness-gated and
            # backend-agnostic. Only succeeds when something IS listening on the port.
            served = (
                await _fetch_inside_response(runtime, conversation_id, target_port, upstream_path)
                if target_port is not None
                else None
            )
            if served is not None:
                return served
            # runthru-v2: the sandbox can't be woken (finished build / backend
            # unavailable), but the built static site may already be on the host
            # snapshot — serve it directly instead of a 503 for a file we have.
            served = _serve_static_from_snapshot(
                runtime,
                conversation_id,
                safe_path,
                version=version,
                entry_path=entry_path,
                inject_selection=preview_cap is not None,
            )
            if served is not None:
                return served
            return Response("preview not available", status_code=503, media_type="text/plain")
        assert target_port is not None  # upstream cannot resolve without a selected port
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                target_url = f"{upstream.rstrip('/')}/{safe_path}"
                if query:
                    target_url = f"{target_url}?{query}"
                r = await client.get(target_url)
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            # Fix 2 safety net: a RESOLVED-but-unreachable upstream (host port mapped but
            # nothing answers) bypassed the None-fallback above; try the in-sandbox
            # liveness proxy before giving up so a live server is still served.
            served = await _fetch_inside_response(
                runtime, conversation_id, target_port, upstream_path
            )
            if served is not None:
                return served
            return Response(
                "preview upstream unreachable", status_code=502, media_type="text/plain"
            )  # noqa: E501
        media_type = r.headers.get("content-type", "text/html")
        return Response(
            content=inject_element_mention_picker(r.content, media_type),
            status_code=r.status_code,
            media_type=media_type,
        )

    @router.websocket("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.websocket("/conversations/{conversation_id}/preview-app/")
    @router.websocket(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/{{path:path}}")
    @router.websocket(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/")
    async def preview_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        path: str = "",
    ) -> None:
        """Proxy live preview WebSockets (Vite HMR) through the path preview URL."""
        session = websocket_session(websocket)
        if session is None:
            (
                owner_id,
                capability_error,
                target_port,
            ) = await _path_preview_websocket_capability_owner(websocket, store, conversation_id)
            if owner_id is None:
                await _close_ws(websocket, 1008, capability_error)
                return
        else:
            try:
                conversation_id = await require_owned_conversation_for_owner(
                    store,
                    conversation_id,
                    session.owner_id,
                    owner_bypass=session.session_id == "test-session",
                )
            except HTTPException:
                await _close_ws(websocket, 1008, "conversation forbidden")
                return
            owner_id = session.owner_id
            target_port = _canonical_preview_port(runtime, conversation_id) if runtime else None
        if runtime is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        if websocket.query_params.get("version") is not None:
            await _close_ws(websocket, 1008, "historical previews are static")
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        if target_port is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        upstream = await _wake_for_preview(runtime, cid8, target_port, owner_id=owner_id)
        if upstream is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)

    return router

"""Live-preview routes — availability, restart, and single-origin upstream proxies."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json as _json
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
from pathlib import PurePosixPath
from typing import Literal

import httpx
import idna
import websockets
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    StatusEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
)
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    MAX_PREVIEW_TARGET_PATH_CHARS,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_APP_HTTP_METHODS,
    PREVIEW_BOOTSTRAP_PATH,
    AuthSession,
    PreviewCapability,
    PreviewCapabilitySigner,
    SessionSigner,
    cookie_header_from_headers,
    local_preview_gateway_host,
    local_preview_gateway_ports,
    origin_matches_request_host,
    path_preview_cookie_name,
    path_preview_host_label,
    preview_ttl_s,
)
from disco.core.env import disco_env
from disco.core.events import Event
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.base import strip_redundant_workspace_prefix
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
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
from ..preview_paths import (
    safe_capability_target_path as _safe_capability_target_path,
)
from ..preview_paths import (
    safe_preview_path as _safe_preview_path,
)
from ..preview_projection import (
    SealedPreviewRuntimeContract,
    derive_sealed_preview_runtime_contract,
)
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
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
    transport: Literal["host", "path", "path_live", "canonical"] = "host"
    workspace_version: int | None = Field(default=None, ge=1)


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


def _verified_snapshot_request_path(
    files: set[str],
    requested_path: str,
    entry_path: str | None,
) -> str | None:
    """Resolve a preview request using only the verified file manifest."""

    requested = _safe_preview_path(requested_path, allow_leading_slash=True)
    if requested is None:
        return None
    directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in files
        for index in range(1, len(PurePosixPath(path).parts))
    }

    if entry_path is None:
        selected = requested or "index.html"
        if selected in directories:
            selected = f"{selected}/index.html"
        return selected if selected in files else None

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
    if base_text and (requested == base_text or requested.startswith(f"{base_text}/")):
        candidate = requested
    else:
        candidate = f"{base_text}/{requested}" if base_text else requested
    if candidate in directories:
        candidate = f"{candidate}/index.html"
    return candidate if candidate in files else None


def _selected_app_entry(  # noqa: ANN001
    events: list,
    *,
    version: int | None,
    marker_seq: int | None = None,
) -> str | None:
    """Return the last app handoff committed by one version marker.

    An unchanged finished workspace may legitimately reuse an immutable version
    number. Version identity alone therefore cannot select the first matching
    marker: doing so revives an older app handoff. Current-preview callers bind
    the lookup to the exact final-seal sequence; historical callers use the most
    recent marker for that immutable version.
    """

    public_markers = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent)
        and event.version_seq == version
        and event.seq is not None
        and not (event.final_seal is None and event.trigger.startswith("finalizing:"))
    ]
    if version is not None and marker_seq is None:
        marker_seq = max(
            (event.seq for event in public_markers if event.seq is not None),
            default=None,
        )
    if version is not None and marker_seq is None:
        return None
    if version is not None and not any(event.seq == marker_seq for event in public_markers):
        return None

    selected: str | None = None
    for event in events:
        if marker_seq is not None and event.seq is not None and event.seq > marker_seq:
            break
        if isinstance(event, DeliverableEvent) and event.artifact_kind == "app":
            selected = event.path
    return selected


def _finished_snapshot_is_committed(events: list) -> bool:  # noqa: ANN001
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
    return urllib.parse.urlencode(pairs, doseq=True)


def _legacy_workspace_version(request: Request) -> int | None:
    values = request.query_params.getlist("version")
    if not values:
        return None
    if len(values) != 1:
        raise HTTPException(status_code=422, detail="invalid workspace version")
    try:
        version = int(values[0])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid workspace version") from exc
    if version < 1:
        raise HTTPException(status_code=422, detail="invalid workspace version")
    return version


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
        if version is not None:
            with ps.open_verified_version(conversation_id, version) as verified:
                target_rel = _verified_snapshot_request_path(
                    {entry.path for entry in verified.files},
                    rel_path,
                    entry_path,
                )
                if target_rel is None:
                    if entry_path is not None:
                        return Response(
                            "preview asset not found",
                            status_code=404,
                            media_type="text/plain",
                        )
                    return None
                body = verified.read_bytes(target_rel)
                ctype = mimetypes.guess_type(target_rel)[0] or "application/octet-stream"
            if inject_selection:
                body = inject_selection_agent(body, ctype)
            body = inject_element_mention_picker(body, ctype)
            return Response(content=body, media_type=ctype)
        ws = ps.path_for(conversation_id).resolve()
    except StorageError:
        if version is not None:
            return Response(
                "committed workspace unavailable" if committed else "version not found",
                status_code=503 if committed else 404,
                media_type="text/plain",
            )
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
    runtime: ConversationRuntime,
    conversation_id: str,
    port: int,
    rel_path: str,
    *,
    inject_selection: bool = False,
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
    if inject_selection:
        body = inject_selection_agent(body, media_type)
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=status,
        media_type=media_type,
    )


async def _serve_active_finished_projection(
    runtime: ConversationRuntime,
    conversation_id: str,
    rel_path: str,
    contract: SealedPreviewRuntimeContract,
    *,
    capability_port: int | None,
    inject_selection: bool = False,
) -> Response | None:
    """Proxy the original generation or its exact host-bound sealed restore."""

    resolver = getattr(runtime, "resolve_finished_preview_runtime", None)
    if resolver is None:
        return None
    resolved = await resolver(conversation_id, contract)
    if not isinstance(resolved, dict):
        return None
    port = resolved.get("port")
    if type(port) is not int or port not in USER_PORTS or port == NOVNC_PORT:
        return None
    if capability_port is not None and capability_port != port:
        return None
    # GET remains passive: only the authenticated restart action may reconstruct a
    # sealed runtime. This path resolves a manager generation already proven live.
    upstream = runtime.port_upstream(conversation_id, port)
    if upstream is None:
        return await _fetch_inside_response(
            runtime,
            conversation_id,
            port,
            rel_path,
            inject_selection=inject_selection,
        )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.get(f"{upstream}/{rel_path}")
    except Exception:  # noqa: BLE001 — a lost original generation is unavailable
        return await _fetch_inside_response(
            runtime,
            conversation_id,
            port,
            rel_path,
            inject_selection=inject_selection,
        )
    media_type = response.headers.get("content-type", "text/html")
    body = response.content
    if inject_selection:
        body = inject_selection_agent(body, media_type)
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=response.status_code,
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


def _local_preview_request(request: Request) -> bool:
    hostname = (request.url.hostname or "").lower().rstrip(".")
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _local_preview_bootstrap_url(request: Request, listener_port: int) -> str:
    """Return a DNS-free loopback origin served by the gateway listener pool."""

    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    scheme = forwarded if forwarded in {"http", "https"} else request.url.scheme
    if scheme != "http":
        # A loopback HTTP listener embedded by an HTTPS UI would be mixed
        # content. Operators terminating TLS use PREVIEW_ORIGIN_BASE instead.
        raise HTTPException(
            status_code=409,
            detail={"reason": "local_preview_https_requires_preview_origin_base"},
        )
    host = local_preview_gateway_host(listener_port)
    return f"http://{host}:{listener_port}{PREVIEW_BOOTSTRAP_PATH}"


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
    workspace_version: int | None = None,
) -> bool:
    """Narrow legacy-8000 namespace for a proven immutable app snapshot only."""
    if runtime is None:
        return False
    version = workspace_version
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing evidence cannot authorize fallback
        return False
    if version is None:
        project_store = runtime.project_store()
        if project_store is None or project_store.status() != StorageStatus.OK:
            return False
        try:
            committed = resolve_committed_workspace(events, project_store, conversation_id)
        except WorkspaceCommitUnavailable:
            return False
        version = committed.event.version_seq
        marker_seq = committed.event.seq
    else:
        marker_seq = None
    entry_path = _selected_app_entry(events, version=version, marker_seq=marker_seq)
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


def _preview_authority_label(kind: str, *parts: object) -> str:
    material = _json.dumps([kind, *parts], separators=(",", ":"), ensure_ascii=True)
    return f"{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _sealed_runtime_contract(
    events: list[Event],
    conversation_id: str,
    committed,  # noqa: ANN001 - CommittedWorkspaceView stays internal to persistence
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


async def _canonical_preview_authority(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    selected_port: int,
    *,
    workspace_version: int | None = None,
) -> str | None:
    """Bind one canonical browser origin to exact live or immutable authority."""

    if runtime is None:
        return None
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — missing authority evidence fails closed
        return None
    if workspace_version is not None:
        version = workspace_version
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
            "version", conversation_id, version, version_event.tree_digest, entry
        )

    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        project_store = runtime.project_store()
        if project_store is None or project_store.status() != StorageStatus.OK:
            return None
        try:
            committed = resolve_committed_workspace(events, project_store, conversation_id)
        except WorkspaceCommitUnavailable:
            return None
        entry = _selected_app_entry(
            events,
            version=committed.event.version_seq,
            marker_seq=committed.event.seq,
        )
        if entry is None:
            return None
        contract = _sealed_runtime_contract(events, conversation_id, committed, entry)
        if contract is not None:
            resolver = getattr(runtime, "resolve_finished_preview_runtime", None)
            resolved = await resolver(conversation_id, contract) if resolver is not None else None
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
            return _preview_authority_label(
                "sealed-runtime",
                conversation_id,
                committed.event.version_seq,
                committed.event.tree_digest,
                contract.contract_id,
                generation,
                runtime_port,
                entry,
            )
        return _preview_authority_label(
            "committed",
            conversation_id,
            committed.event.version_seq,
            committed.event.tree_digest,
            entry,
        )

    try:
        metadata = await runtime.preview(conversation_id)
    except Exception:  # noqa: BLE001 — lifecycle authority cannot be guessed
        return None
    generation = metadata.get("generation") if isinstance(metadata, dict) else None
    metadata_port = metadata.get("port") if isinstance(metadata, dict) else None
    if not isinstance(generation, str) or not generation or metadata_port != selected_port:
        return None
    entry = _selected_app_entry(events, version=None)
    return _preview_authority_label("live", conversation_id, generation, selected_port, entry or "")


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
        bootstrap = _local_preview_bootstrap_url(request, lease.listener_port)
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
    )
    return bootstrap, intent, authority_id, workspace_version


async def _canonical_preview_workspace_version(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    conversation_id: str,
    request: Request,
    preview_cap: object | None,
) -> tuple[int | None, Response | None]:
    canonical = (
        preview_cap
        if isinstance(preview_cap, PreviewCapability) and preview_cap.authority_id is not None
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
        return Response("committed preview unavailable", status_code=503, media_type="text/plain")
    return Response("version not found", status_code=404, media_type="text/plain")


async def _preview_capability_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    cap_signer: PreviewCapabilitySigner,
    conversation_id: str,
    body: PreviewCapabilityBody,
    request: Request,
) -> Response:
    if body.port is not None and body.port not in USER_PORTS:
        raise HTTPException(status_code=404, detail={"reason": "unknown_port"})
    if body.workspace_version is not None and body.transport != "canonical":
        raise HTTPException(
            status_code=400,
            detail={"reason": "workspace_version_requires_canonical_preview"},
        )
    session = current_session(request)
    conversation_id = await require_owned_conversation(request, store, conversation_id)
    cid8 = conversation_id.removeprefix("conv_")[:8]
    target = body.target_path if body.target_path.startswith("/") else f"/{body.target_path}"
    target_parts = urllib.parse.urlsplit(target)
    if target_parts.scheme or target_parts.netloc:
        raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
    safe_target = _safe_capability_target_path(target_parts.path)
    if safe_target is None:
        raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
    if body.transport in {"path", "path_live", "canonical"}:
        selected_port = (
            _canonical_preview_port(runtime, conversation_id)
            if runtime is not None
            else PREVIEW_PORT
        )
        if selected_port is None and body.transport == "canonical" and runtime is not None:
            # Capability mint is an authenticated POST and therefore an explicit
            # recovery boundary. It may restore one sealed managed runtime; the
            # subsequent authority check still binds the fresh generation.
            ensure = getattr(runtime, "ensure_preview", None)
            if ensure is not None:
                await ensure(conversation_id)
                selected_port = _canonical_preview_port(runtime, conversation_id)
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
            raise HTTPException(status_code=409, detail={"reason": "preview_unavailable"})
        if body.port is not None and body.port != selected_port:
            raise HTTPException(status_code=400, detail={"reason": "path_preview_port_mismatch"})
        port = selected_port
    else:
        port = body.port if body.port is not None else PREVIEW_PORT

    authority_id: str | None = None
    immutable_version: int | None = None
    if body.transport == "canonical":
        bootstrap, intent, authority_id, immutable_version = await _mint_canonical_preview(
            store=store,
            runtime=runtime,
            request=request,
            session=session,
            signer=cap_signer,
            conversation_id=conversation_id,
            cid8=cid8,
            port=port,
            target=target,
            workspace_version=body.workspace_version,
        )
    elif body.transport == "host":
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
            "preview_authority": authority_id,
            "immutable_version": immutable_version,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


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
        return await _preview_capability_response(
            store, runtime, cap_signer, conversation_id, body, request
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
        target_path = urllib.parse.urlsplit(target).path
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


def _register_preview_meta_routes(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    """Register preview availability and restart routes."""

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


async def _finished_preview_response(
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    safe_path: str,
    events: list[Event],
    preview_cap: PreviewCapability | None,
) -> Response:
    project_store = runtime.project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        return Response(
            "finished workspace is not sealed", status_code=503, media_type="text/plain"
        )
    try:
        committed = resolve_committed_workspace(events, project_store, conversation_id)
    except WorkspaceCommitUnavailable:
        return Response(
            "finished workspace is finalizing or unsealed",
            status_code=503,
            media_type="text/plain",
        )
    committed_version = committed.event.version_seq
    entry_path = _selected_app_entry(
        events,
        version=committed_version,
        marker_seq=committed.event.seq,
    )
    contract = (
        _sealed_runtime_contract(events, conversation_id, committed, entry_path)
        if entry_path is not None
        else None
    )
    if contract is not None:
        query = _forwarded_preview_query(request)
        active_path = f"{safe_path}?{query}" if query else safe_path
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
            "sealed runtime preview unavailable", status_code=503, media_type="text/plain"
        )
    served = _serve_static_from_snapshot(
        runtime,
        conversation_id,
        safe_path,
        version=committed_version,
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
        await _wake_for_preview(runtime, cid8, target_port, owner_id=owner_id)
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
        if served is None:
            # An active owned build has one application authority: its managed
            # runtime.  Mutable workspace bytes are not a substitute for a down
            # Vite/Next/Node process (and may be raw source or a stale scaffold).
            # Keep immutable static serving for FINISHED/historical routes only.
            served = None
        return served or Response("preview not available", status_code=503, media_type="text/plain")
    assert target_port is not None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            target_url = f"{upstream.rstrip('/')}/{safe_path}"
            if query:
                target_url = f"{target_url}?{query}"
            response = await client.get(target_url)
    except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
        served = await _fetch_inside_response(
            runtime,
            conversation_id,
            target_port,
            upstream_path,
            inject_selection=preview_cap is not None,
        )
        return served or Response(
            "preview upstream unreachable", status_code=502, media_type="text/plain"
        )
    media_type = response.headers.get("content-type", "text/html")
    body = response.content
    if preview_cap is not None:
        body = inject_selection_agent(body, media_type)
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=response.status_code,
        media_type=media_type,
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
        return Response("invalid preview capability", status_code=403, media_type="text/plain")
    if preview_cap is None:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        owner_id = current_session(request).owner_id
    else:
        if preview_cap.conversation_id != conversation_id:
            return Response("preview capability mismatch", status_code=403, media_type="text/plain")
        owner_id = preview_cap.owner_id
    if runtime is None:
        return Response("preview not available", status_code=503, media_type="text/plain")
    safe_path = _safe_preview_path(path, allow_leading_slash=True)
    if safe_path is None or is_runtime_secret_path(safe_path):
        return Response("preview path not found", status_code=404, media_type="text/plain")
    try:
        events = await store.get_events(conversation_id)
    except Exception:  # noqa: BLE001 — selected-target metadata is security relevant
        _LOG.warning("preview target metadata unavailable for %s", conversation_id, exc_info=True)
        return Response("preview metadata unavailable", status_code=503, media_type="text/plain")
    workspace_version, authority_error = await _canonical_preview_workspace_version(
        store, runtime, conversation_id, request, preview_cap
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
            runtime, request, conversation_id, safe_path, events, preview_cap
        )
    return await _active_preview_response(
        runtime, request, conversation_id, safe_path, events, preview_cap, owner_id
    )


def _register_preview_app_routes(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    """Register the preview-app GET proxy routes and their historical-version variants."""

    @router.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.get("/conversations/{conversation_id}/preview-app/")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/{{path:path}}")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/")
    async def preview_app(
        request: Request,
        conversation_id: str,
        path: str = "",
    ) -> Response:
        return await _preview_app_response(store, runtime, request, conversation_id, path)


def _register_preview_app_websocket_routes(
    router: APIRouter, store: SqliteEventStore, runtime: ConversationRuntime | None
) -> None:
    """Register the preview-app WebSocket proxy routes."""

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
        canonical_cap = websocket.scope.get("state", {}).get("canonical_preview_capability")
        session = websocket_session(websocket)
        if isinstance(canonical_cap, PreviewCapability) and canonical_cap.authority_id is not None:
            if canonical_cap.conversation_id != conversation_id:
                await _close_ws(websocket, 1008, "preview capability mismatch")
                return
            stored_owner = await store.conversation_owner_id(conversation_id)
            if stored_owner is None or stored_owner != canonical_cap.owner_id:
                await _close_ws(websocket, 1008, "conversation forbidden")
                return
            owner_id = canonical_cap.owner_id
            target_port = canonical_cap.port
        elif session is None:
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
        if (
            isinstance(canonical_cap, PreviewCapability)
            and canonical_cap.immutable_version is not None
        ):
            await _close_ws(websocket, 1008, "historical previews are static")
            return
        try:
            events = await store.get_events(conversation_id)
        except Exception:  # noqa: BLE001 — status uncertainty cannot authorize a wake
            await _close_ws(websocket, 1008, "preview metadata unavailable")
            return
        if isinstance(canonical_cap, PreviewCapability):
            canonical_port = canonical_cap.port
            current_authority = await _canonical_preview_authority(
                store,
                runtime,
                conversation_id,
                canonical_port,
                workspace_version=canonical_cap.immutable_version,
            )
            if current_authority != canonical_cap.authority_id:
                await _close_ws(websocket, 1008, "preview generation changed")
                return
        latest_status = next(
            (event for event in reversed(events) if isinstance(event, StatusEvent)),
            None,
        )
        if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
            # FINISHED WebSockets obey the same no-wake boundary as HTTP.  They
            # may use only the exact active-live generation bound to the current
            # final seal; filtered backends without a passive host upstream close
            # honestly because fetch_inside cannot proxy a WebSocket.
            project_store = runtime.project_store()
            if project_store is None or project_store.status() != StorageStatus.OK:
                await _close_ws(websocket, 1008, "preview not available")
                return
            try:
                committed = resolve_committed_workspace(
                    events,
                    project_store,
                    conversation_id,
                )
            except WorkspaceCommitUnavailable:
                await _close_ws(websocket, 1008, "preview not available")
                return
            entry = _selected_app_entry(
                events,
                version=committed.event.version_seq,
                marker_seq=committed.event.seq,
            )
            contract = (
                _sealed_runtime_contract(events, conversation_id, committed, entry)
                if entry is not None
                else None
            )
            resolver = getattr(runtime, "resolve_finished_preview_runtime", None)
            resolved = (
                await resolver(conversation_id, contract)
                if resolver is not None and contract is not None
                else None
            )
            runtime_port = resolved.get("port") if isinstance(resolved, dict) else None
            if contract is None or type(runtime_port) is not int or target_port != runtime_port:
                await _close_ws(websocket, 1008, "preview not available")
                return
            upstream = runtime.port_upstream(conversation_id, runtime_port)
            if upstream is None:
                await _close_ws(websocket, 1008, "preview not available")
                return
            await _proxy_websocket_to_upstream(websocket, upstream, path)
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


def make_preview_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()
    _register_preview_capability_route(router, store, runtime)
    _register_live_browser_start_route(router, store, runtime)
    _register_live_browser_status_routes(router, store, runtime)
    _register_port_preview_routes(router, store, runtime)
    _register_preview_meta_routes(router, store, runtime)
    _register_preview_app_routes(router, store, runtime)
    _register_preview_app_websocket_routes(router, store, runtime)
    return router

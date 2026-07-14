"""Live-preview routes — availability, restart, and single-origin upstream proxies."""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import mimetypes
import secrets
import urllib.parse
from pathlib import PurePosixPath

import httpx
import websockets
from disco.core import ConversationStatus, DeliverableEvent, StatusEvent, WorkspaceVersionEvent
from disco.core.auth import (
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_BOOTSTRAP_PATH,
    PreviewCapabilitySigner,
    path_preview_cookie_name,
    preview_ttl_s,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus, is_runtime_secret_path
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.base import strip_redundant_workspace_prefix
from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..auth import current_session, websocket_session
from ..preview_inject import inject_element_mention_picker, inject_selection_agent
from ..runtime import ConversationRuntime
from ._common import (
    require_owned_conversation,
    require_owned_conversation_for_owner,
)

_LOG = logging.getLogger(__name__)


class PreviewCapabilityBody(BaseModel):
    port: int = PREVIEW_PORT
    target_path: str = "/"


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


def _preview_bootstrap_url(request: Request, cid8: str, port: int, intent: str) -> str:
    url = request.url
    hostname = url.hostname or "localhost"
    if hostname in {"127.0.0.1", "localhost"}:
        preview_host = f"{cid8}-{port}.localhost"
    else:
        preview_host = f"{cid8}-{port}.{hostname}"
    netloc = preview_host
    if url.port is not None:
        netloc = f"{preview_host}:{url.port}"
    # Behind the front-door nginx (and any OUTER TLS terminator ahead of it) the
    # page scheme travels in X-Forwarded-Proto; request.url.scheme is only the
    # last plain-HTTP hop. Minting http:// on an https page would be blocked as
    # mixed content (codex front-door defect #2, 2026-07-09).
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = fwd_proto if fwd_proto in {"http", "https"} else url.scheme
    query = urllib.parse.urlencode({"intent": intent})
    return urllib.parse.urlunparse((scheme, netloc, PREVIEW_BOOTSTRAP_PATH, "", query, ""))


def _path_preview_bootstrap_url(request: Request, cid8: str, port: int, intent: str) -> str:
    """Return a Firefox-safe isolated origin for committed static previews."""
    url = request.url
    hostname = url.hostname or "localhost"
    if hostname in {"127.0.0.1", "::1"}:
        preview_host = "localhost"
    elif hostname == "localhost":
        preview_host = "127.0.0.1"
    else:
        # Remote front doors already route this capability-gated wildcard host
        # to the agent server. HostPreviewProxyMiddleware passes the dedicated
        # static path routes through instead of treating them as a live port.
        preview_host = f"{cid8}-{port}.{hostname}"
    netloc = preview_host if url.port is None else f"{preview_host}:{url.port}"
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = fwd_proto if fwd_proto in {"http", "https"} else url.scheme
    query = urllib.parse.urlencode({"intent": intent})
    path = f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}"
    return urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))


def _locked_preview_navigation(destination: str) -> Response:
    """Return a script-only document that starts a fresh navigation on this origin."""
    nonce = secrets.token_urlsafe(18)
    safe_destination_json = (
        _json.dumps(destination)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    document = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<title>Opening isolated preview</title></head><body>"
        f"<script nonce=\"{nonce}\">window.location.replace({safe_destination_json})</script>"
        "<noscript>JavaScript is required to open this isolated preview.</noscript>"
        "</body></html>"
    )
    return Response(
        document,
        status_code=200,
        media_type="text/html",
        headers={
            "content-security-policy": (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors *"
            ),
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "cache-control": "no-store",
        },
    )


async def _wake_for_preview(
    runtime: ConversationRuntime, cid8: str, port: int, *, owner_id: str
) -> str | None:
    try:
        return await runtime.wake_for_preview(cid8, port, owner_id=owner_id)
    except TypeError:
        return await runtime.wake_for_preview(cid8, port)  # type: ignore[call-arg]


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
        ws_client = await websockets.connect(target_url, subprotocols=subprotocols)
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


def _register_preview_capability_route(router: APIRouter, store: SqliteEventStore) -> None:
    cap_signer = PreviewCapabilitySigner()

    @router.post("/conversations/{conversation_id}/preview/capability")
    async def preview_capability(
        conversation_id: str,
        body: PreviewCapabilityBody,
        request: Request,
    ) -> dict:
        if body.port not in USER_PORTS:
            raise HTTPException(status_code=404, detail={"reason": "unknown_port"})
        session = current_session(request)
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        cid8 = conversation_id.removeprefix("conv_")[:8]
        target = body.target_path if body.target_path.startswith("/") else f"/{body.target_path}"
        intent = cap_signer.mint_intent(
            session=session,
            conversation_id=conversation_id,
            port=body.port,
            target_path=target,
        )
        bootstrap = _preview_bootstrap_url(request, cid8, body.port, intent)
        target_parts = urllib.parse.urlsplit(target)
        if target_parts.scheme or target_parts.netloc or target_parts.fragment:
            raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
        safe_target = _safe_preview_path(target_parts.path, allow_leading_slash=True)
        if safe_target is None:
            raise HTTPException(status_code=400, detail={"reason": "invalid_preview_path"})
        path_prefix = f"/conversations/{conversation_id}/preview-app/"
        path_target = f"{path_prefix}{safe_target}"
        if target_parts.query:
            path_target = f"{path_target}?{target_parts.query}"
        path_bootstrap = None
        if body.port == PREVIEW_PORT:
            path_intent = cap_signer.mint_intent(
                session=session,
                conversation_id=conversation_id,
                port=body.port,
                target_path=path_target,
                path_prefix=path_prefix,
            )
            path_bootstrap = _path_preview_bootstrap_url(
                request, cid8, body.port, path_intent
            )
        return {
            "bootstrap_url": bootstrap,
            "path_bootstrap_url": path_bootstrap,
            "target_path": target,
            "port": body.port,
        }

    @router.get(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{{cid8}}")
    async def path_preview_bootstrap(cid8: str, request: Request) -> Response:
        if len(cid8) != 8 or any(ch not in "0123456789abcdef" for ch in cid8.lower()):
            return Response("invalid preview target", status_code=403, media_type="text/plain")
        minted = cap_signer.mint_cookie_from_intent(request.query_params.get("intent", ""))
        if minted is None:
            return Response("invalid preview intent", status_code=403, media_type="text/plain")
        token, target = minted
        target_path = target.split("?", 1)[0]
        cap = cap_signer.verify(
            token,
            cid8=cid8,
            port=PREVIEW_PORT,
            method="GET",
            path=target_path,
        )
        expected_prefix = (
            f"/conversations/{cap.conversation_id}/preview-app/" if cap is not None else ""
        )
        if cap is None or not target_path.startswith(expected_prefix):
            return Response(
                "preview intent scope mismatch", status_code=403, media_type="text/plain"
            )
        # H084: a Strict cookie set by the first 127.0.0.1 -> localhost iframe
        # response is not reliably accepted at all; merely replacing a 303 with
        # a 200 document is insufficient. Land on the isolated origin first
        # without setting a cookie. Its script then redeems the same short-lived
        # signed intent through a second, now same-site document. Only that
        # response installs the narrow capability before navigating to target.
        stage = request.query_params.get("stage")
        if stage is None:
            redeem_query = urllib.parse.urlencode(
                {"intent": request.query_params.get("intent", ""), "stage": "redeem"}
            )
            redeem_target = f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}?{redeem_query}"
            return _locked_preview_navigation(redeem_target)
        if stage != "redeem":
            return Response("invalid preview stage", status_code=403, media_type="text/plain")

        response = _locked_preview_navigation(target)
        forwarded_scheme = (request.headers.get("x-forwarded-proto") or "").split(",")[
            0
        ].strip()
        response.set_cookie(
            path_preview_cookie_name(cid8),
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


def make_preview_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()
    _register_preview_capability_route(router, store)
    _register_live_browser_start_route(router, store, runtime)
    _register_live_browser_status_routes(router, store, runtime)

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
        upstream = await _wake_for_preview(runtime, cid8, PREVIEW_PORT, owner_id=owner_id)
        query = _forwarded_preview_query(request)
        upstream_path = f"{safe_path}?{query}" if query else safe_path
        if upstream is None:
            # Fix 2 (B-E): on sealed/filtered boxes no host port is published, so
            # `wake_for_preview` resolves None even with a live dev server. Before
            # falling back to the (stale mid-run) snapshot, try a liveness proxy that
            # curls the server from INSIDE the sandbox — genuinely liveness-gated and
            # backend-agnostic. Only succeeds when something IS listening on the port.
            served = await _fetch_inside_response(
                runtime, conversation_id, PREVIEW_PORT, upstream_path
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
                runtime, conversation_id, PREVIEW_PORT, upstream_path
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
    async def preview_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        path: str = "",
    ) -> None:
        """Proxy live preview WebSockets (Vite HMR) through the path preview URL."""
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
        if runtime is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        if websocket.query_params.get("version") is not None:
            await _close_ws(websocket, 1008, "historical previews are static")
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await _wake_for_preview(runtime, cid8, PREVIEW_PORT, owner_id=session.owner_id)
        if upstream is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)

    @router.get("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.get("/conversations/{conversation_id}/port/{port}/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages.
    # WALK-10: now wakes suspended sandboxes via wake_for_preview — same fix as preview_app.
    async def port_app(
        request: Request, conversation_id: str, port: int, path: str = ""
    ) -> Response:
        """Per-port proxy (BP-10): same single-origin forwarding as preview-app for
        the curated USER port set. Arbitrary ints and INTERNAL plumbing ports are
        never proxied (404 — not 503: the port does not exist as a surface).

        Uses wake_for_preview so a suspended sandbox is rematerialised on demand."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if port not in USER_PORTS:
            return Response("unknown port", status_code=404, media_type="text/plain")
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        owner_id = current_session(request).owner_id
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await _wake_for_preview(runtime, cid8, port, owner_id=owner_id)
        if upstream is None:
            # Fix 2 (B-E): liveness proxy into the sandbox when no host port is
            # published (sealed/filtered boxes). Honest 503 if nothing is listening.
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
            if served is not None:
                return served
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                r = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            # Fix 2 safety net: resolved-but-unreachable upstream → try the in-sandbox
            # liveness proxy before the 502 (same as preview_app).
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
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

    @router.websocket("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.websocket("/conversations/{conversation_id}/port/{port}/")
    async def port_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        port: int,
        path: str = "",
    ) -> None:
        """Proxy live curated-port WebSockets (including Vite HMR)."""
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

    return router

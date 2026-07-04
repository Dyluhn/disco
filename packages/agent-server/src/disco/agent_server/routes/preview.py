"""Live-preview routes — availability, restart, and single-origin upstream proxies."""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import mimetypes
import urllib.parse

import httpx
import websockets
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageError, StorageStatus
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.base import strip_redundant_workspace_prefix
from fastapi import APIRouter, Query, Response, WebSocket
from fastapi.responses import JSONResponse

from ..preview_inject import inject_element_mention_picker
from ..runtime import ConversationRuntime

_LOG = logging.getLogger(__name__)


def _serve_static_from_snapshot(
    runtime: ConversationRuntime,
    conversation_id: str,
    rel_path: str,
    *,
    version: int | None = None,
) -> Response | None:
    """runthru-v2: serve a FINISHED build's static site DIRECTLY from the host
    ProjectStore snapshot when the sandbox can't be woken (build finished + reaped,
    or the configured backend is unavailable). The built files already sit on disk
    at projects/{cid}/workspace/ — returning a 503 for a file we HAVE is the bug the
    user hit ("preview not available" on a completed app). Jailed to the snapshot
    workspace (mirrors files.py), normalizes a redundant 'workspace/' prefix."""
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
    rel = strip_redundant_workspace_prefix(rel_path or "").strip("/") or "index.html"
    target = (ws / rel).resolve()
    if target.is_dir():
        target = (target / "index.html").resolve()
    if not (target.is_relative_to(ws) and target.is_file()):
        return None
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    body = inject_element_mention_picker(target.read_bytes(), ctype)
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


async def _proxy_websocket_to_upstream(
    websocket: WebSocket, upstream: str, rel_path: str
) -> None:
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


def make_preview_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/browser/live-url")
    async def browser_live_url(conversation_id: str) -> Response:
        """Lazily start the noVNC live-view stack in the sandbox and return the
        auth-gated proxy URL. Returns 503 when live browser is disabled in Settings
        or when no sandbox is running for this conversation.

        Security: the noVNC endpoint is behind the existing per-conversation
        preview proxy ({cid8}-{NOVNC_PORT}.localhost) — same owner-scoped auth
        as the dev-server preview. VNC is loopback-bound inside the sandbox.

        P5 live jail acceptance is HARDWARE-DEFERRED (VM 201 destroyed). The
        security invariants (loopback-bind, per-conv jail, view-only) must be
        verified on a real sandbox backend before shipping to production."""
        if runtime is None:
            return Response("no runtime", status_code=503, media_type="text/plain")

        # Check if live browser is enabled in config
        try:
            cfg = runtime._config_store.load()
            if not cfg.live_browser.enabled:
                return Response(
                    _json.dumps({
                        "reason": "disabled",
                        "message": "Live browser is off — enable it in Settings → Agent.",
                    }),
                    status_code=503,
                    media_type="application/json",
                )
        except Exception:
            return Response("config unavailable", status_code=503, media_type="text/plain")

        cid8 = conversation_id.removeprefix("conv_")[:8]

        # Trigger live_start inside the sandbox via the browser daemon
        session = runtime.live_session(conversation_id)
        if session is None:
            return Response(
                _json.dumps({
                    "reason": "no_sandbox",
                    "message": "No sandbox running for this conversation — start the agent first.",
                }),
                status_code=503,
                media_type="application/json",
            )

        # HONESTY GATE (fixes the live_start_failed bug): only a backend that ships the
        # Xvfb/x11vnc/websockify stack (gVisor — see LIVE_VIEW_BACKENDS) can run the live
        # view. On the process dev / local / podman backends live_start would shell out to
        # an Xvfb binary that isn't there and fail with "Failed to start live view stack".
        # Refuse up-front with an honest reason so the frontend falls back to screenshots
        # rather than surfacing a scary failure. live-ready reports the same truth, so the
        # auto-stream path never even reaches here on an unsupported backend.
        if not getattr(session, "supports_live_view", False):
            return Response(
                _json.dumps({
                    "reason": "unsupported_backend",
                    "message": "Live browser needs the gVisor sandbox — this backend can't run it.",
                }),
                status_code=503,
                media_type="application/json",
            )

        try:
            # Check browser daemon health first
            res = await session.exec_shell(
                "curl -sf http://127.0.0.1:8901/health",
                timeout_s=3,
            )
            if res.exit_code != 0:
                return Response(
                    _json.dumps({
                        "reason": "no_daemon",
                        "message": "Browser daemon not running — use the browser tool first.",
                    }),
                    status_code=503,
                    media_type="application/json",
                )

            # Trigger live_start in the daemon
            job = _json.dumps({"action": "live_start"})
            # Escape single quotes for shell safety
            job_escaped = job.replace("'", "'\"'\"'")
            res2 = await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json'"
                f" -d '{job_escaped}'",
                timeout_s=30,
            )
            if res2.exit_code != 0:
                return Response(
                    _json.dumps({
                        "reason": "live_start_failed",
                        "message": "Failed to start live view stack.",
                    }),
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

        # Readiness check ONLY: confirm the sandbox has actually published NOVNC_PORT
        # (wake_for_preview → port_upstream → expose_port resolves the host mapping).
        # We deliberately DISCARD the resolved value: it is the raw sandbox host:port
        # upstream, and handing that to the browser would bypass HostPreviewProxyMiddleware
        # (cid/owner scoping + the live-browser enabled-gate). With x11vnc -nopw, a leaked
        # raw URL is enough to watch the session — so the proxy must be the ONLY
        # browser-visible path. (noVNC BLOCK fix.)
        upstream = await runtime.wake_for_preview(cid8, NOVNC_PORT)
        if upstream is None:
            return Response(
                _json.dumps({
                    "reason": "no_upstream",
                    "message": "noVNC port not yet exposed by the sandbox.",
                }),
                status_code=503,
                media_type="application/json",
            )

        # Return only the port; the client builds the single-origin proxy URL
        # ({cid8}-6080.localhost via previewHostUrl), the same path as the dev-server
        # preview. `ready` lets the client distinguish "go" from a 503 without a URL.
        return JSONResponse({
            "ready": True,
            "novnc_path": "/vnc.html?autoconnect=1&view_only=1",
            "port": NOVNC_PORT,
        })

    @router.get("/conversations/{conversation_id}/browser/live-ready")
    async def browser_live_ready(conversation_id: str) -> Response:
        """W-47: side-effect-FREE readiness probe for the Live button. Mirrors the
        guard checks of /browser/live-url (runtime present → feature enabled → a sandbox
        session exists → the browser daemon is healthy) but triggers NO live_start and
        exposes NO port, so the frontend can POLL it every few seconds to gate the button
        WITHOUT the error-spam and side-effects of calling live-url. Always 200; `ready`
        plus a machine `reason` (which doubles as the button tooltip) carry the state.

        The daemon /health is a read-only GET (returns 200 once the agent has actually
        opened the browser), so this never starts the headed stack or maps a host port."""
        if runtime is None:
            return JSONResponse({"ready": False, "reason": "no_runtime"})

        try:
            cfg = runtime._config_store.load()
            if not cfg.live_browser.enabled:
                return JSONResponse({"ready": False, "reason": "disabled"})
        except Exception:  # noqa: BLE001 — config unavailable is "not ready", never a 500
            return JSONResponse({"ready": False, "reason": "config_unavailable"})

        # Read-only accessor (live_session never CREATES a sandbox — see runtime.py).
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ready": False, "reason": "no_sandbox"})

        # HONESTY GATE: a backend that can't run the Xvfb/x11vnc/websockify stack is NOT
        # streamable — report it as such (NOT-ready) so the frontend shows screenshots and
        # never auto-starts a doomed stack (the live_start_failed case). Only gVisor ships
        # the stack + carries the accepted live-jail security model (LIVE_VIEW_BACKENDS).
        if not getattr(session, "supports_live_view", False):
            return JSONResponse({"ready": False, "reason": "unsupported_backend"})

        # Probe the browser daemon's read-only /health endpoint ONLY. No live_start POST,
        # no wake_for_preview/port expose — a poll must have zero side-effects.
        try:
            res = await session.exec_shell(
                "curl -sf http://127.0.0.1:8901/health",
                timeout_s=3,
            )
        except Exception:  # noqa: BLE001 — a readiness probe must never 500
            return JSONResponse({"ready": False, "reason": "no_daemon"})
        if res.exit_code != 0:
            return JSONResponse({"ready": False, "reason": "no_daemon"})

        return JSONResponse({"ready": True, "reason": "ready"})

    @router.post("/conversations/{conversation_id}/browser/live-touch")
    async def browser_live_touch(conversation_id: str) -> Response:
        """Heartbeat from the open Live pane — refresh the sandbox idle watchdog so an
        actively-watched session is not reaped after the idle timeout. Best-effort + 200
        regardless (a missing sandbox/daemon just means nothing to keep alive)."""
        if runtime is None:
            return JSONResponse({"ok": True, "note": "no runtime"})
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ok": True, "note": "no sandbox"})
        try:
            job = _json.dumps({"action": "live_touch"})
            job_escaped = job.replace("'", "'\"'\"'")
            await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json'"
                f" -d '{job_escaped}'",
                timeout_s=5,
            )
        except Exception:  # noqa: BLE001 — heartbeat is best-effort
            return JSONResponse({"ok": True, "note": "touch best-effort"})
        return JSONResponse({"ok": True})

    @router.post("/conversations/{conversation_id}/browser/live-stop")
    async def browser_live_stop(conversation_id: str) -> Response:
        """Tear the live-view stack down (Xvfb + x11vnc + websockify) inside the sandbox.
        The client calls this when the user closes the Live pane / unmounts / disables
        the feature, so the VNC surface does not linger for the life of the sandbox
        (the idle watchdog is the backstop; this is the prompt path). Always 200 — a
        no-op teardown (no sandbox / no daemon) is success, not an error."""
        if runtime is None:
            return JSONResponse({"ok": True, "note": "no runtime"})
        session = runtime.live_session(conversation_id)
        if session is None:
            return JSONResponse({"ok": True, "note": "no sandbox"})
        try:
            job = _json.dumps({"action": "live_stop"})
            job_escaped = job.replace("'", "'\"'\"'")
            await session.exec_shell(
                f"curl -s -X POST http://127.0.0.1:8901"
                f" -H 'Content-Type: application/json'"
                f" -d '{job_escaped}'",
                timeout_s=10,
            )
        except Exception:  # noqa: BLE001 — best-effort teardown; report success regardless
            return JSONResponse({"ok": True, "note": "teardown best-effort"})
        return JSONResponse({"ok": True})

    @router.get("/conversations/{conversation_id}/preview")
    async def get_preview(conversation_id: str) -> dict:
        """Backend-aware live preview availability (the browser iframes the proxy below)."""
        if runtime is None:
            return {"available": False, "reason": "no runtime"}
        return await runtime.preview(conversation_id)

    @router.post("/conversations/{conversation_id}/preview/restart")
    async def ensure_preview(conversation_id: str) -> dict:
        """Bring a down preview back on demand (§E7) — the UI 'Restart preview' button.
        Bounded + safe (same path as SandboxSession.ensure_preview)."""
        if runtime is None:
            return {"ok": False}
        return {"ok": await runtime.ensure_preview(conversation_id)}

    @router.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.get("/conversations/{conversation_id}/preview-app/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages.
    # WALK-10: now wakes suspended sandboxes via wake_for_preview — fixes the Open button
    # and the PreviewPane "Open in new tab" link that returned 503 after sandbox auto-suspend.
    async def preview_app(
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
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        cid8 = conversation_id.removeprefix("conv_")[:8]
        # Historical preview is static-only AND must NEVER fall through to the live
        # proxy: a ?version request answered by the live sandbox would show current
        # bytes under a "viewing vN" banner — a false affordance. Version requests
        # serve from the version snapshot or 404, full stop.
        if version is not None:
            served = _serve_static_from_snapshot(
                runtime, conversation_id, path, version=version
            )
            if served is not None:
                return served
            return Response("version not found", status_code=404, media_type="text/plain")
        upstream = await runtime.wake_for_preview(cid8, PREVIEW_PORT)
        if upstream is None:
            # Fix 2 (B-E): on sealed/filtered boxes no host port is published, so
            # `wake_for_preview` resolves None even with a live dev server. Before
            # falling back to the (stale mid-run) snapshot, try a liveness proxy that
            # curls the server from INSIDE the sandbox — genuinely liveness-gated and
            # backend-agnostic. Only succeeds when something IS listening on the port.
            served = await _fetch_inside_response(runtime, conversation_id, PREVIEW_PORT, path)
            if served is not None:
                return served
            # runthru-v2: the sandbox can't be woken (finished build / backend
            # unavailable), but the built static site may already be on the host
            # snapshot — serve it directly instead of a 503 for a file we have.
            served = _serve_static_from_snapshot(
                runtime, conversation_id, path, version=version
            )
            if served is not None:
                return served
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            # Fix 2 safety net: a RESOLVED-but-unreachable upstream (host port mapped but
            # nothing answers) bypassed the None-fallback above; try the in-sandbox
            # liveness proxy before giving up so a live server is still served.
            served = await _fetch_inside_response(runtime, conversation_id, PREVIEW_PORT, path)
            if served is not None:
                return served
            return Response("preview upstream unreachable", status_code=502, media_type="text/plain")  # noqa: E501
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
        if runtime is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        if websocket.query_params.get("version") is not None:
            await _close_ws(websocket, 1008, "historical previews are static")
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await runtime.wake_for_preview(cid8, PREVIEW_PORT)
        if upstream is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)

    @router.get("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.get("/conversations/{conversation_id}/port/{port}/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages.
    # WALK-10: now wakes suspended sandboxes via wake_for_preview — same fix as preview_app.
    async def port_app(conversation_id: str, port: int, path: str = "") -> Response:
        """Per-port proxy (BP-10): same single-origin forwarding as preview-app for
        the curated USER port set. Arbitrary ints and INTERNAL plumbing ports are
        never proxied (404 — not 503: the port does not exist as a surface).

        Uses wake_for_preview so a suspended sandbox is rematerialised on demand."""
        if port not in USER_PORTS:
            return Response("unknown port", status_code=404, media_type="text/plain")
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await runtime.wake_for_preview(cid8, port)
        if upstream is None:
            # Fix 2 (B-E): liveness proxy into the sandbox when no host port is
            # published (sealed/filtered boxes). Honest 503 if nothing is listening.
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
            if served is not None:
                return served
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            # Fix 2 safety net: resolved-but-unreachable upstream → try the in-sandbox
            # liveness proxy before the 502 (same as preview_app).
            served = await _fetch_inside_response(runtime, conversation_id, port, path)
            if served is not None:
                return served
            return Response("preview upstream unreachable", status_code=502, media_type="text/plain")  # noqa: E501
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
        if port not in USER_PORTS:
            await _close_ws(websocket, 1008, "unknown port")
            return
        if runtime is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await runtime.wake_for_preview(cid8, port)
        if upstream is None:
            await _close_ws(websocket, 1008, "preview not available")
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)

    return router

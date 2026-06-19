"""Live-preview routes — availability, restart, and single-origin upstream proxies."""

from __future__ import annotations

import json as _json

import httpx
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime


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
                        "message": "Live browser is off — enable it in Settings → Agent → Live browser.",
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
    async def preview_app(conversation_id: str, path: str = "") -> Response:
        """Proxy the agent's dev server through THIS (tailnet-reachable) origin — the
        backend-derived upstream (localhost for local, the remote tailnet IP for gVisor) is
        reached server-side, so no random container port is exposed and previews work over
        the tailnet. Forwards GET; good for a built page (single-origin assets).

        Uses wake_for_preview so a suspended sandbox is rematerialised on demand —
        the passive preview_upstream check only finds live in-memory executors."""
        if runtime is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await runtime.wake_for_preview(cid8, PREVIEW_PORT)
        if upstream is None:
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            return Response("preview upstream unreachable", status_code=502, media_type="text/plain")  # noqa: E501
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "text/html"),
        )

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
            return Response("preview not available", status_code=503, media_type="text/plain")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(f"{upstream}/{path}")
        except Exception:  # noqa: BLE001 — upstream not up yet / unreachable
            return Response("preview upstream unreachable", status_code=502, media_type="text/plain")  # noqa: E501
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "text/html"),
        )

    return router

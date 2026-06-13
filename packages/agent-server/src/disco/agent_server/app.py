"""The agent-server wire + REST surface — event-state-contract.md §7.

A thin adapter over the core `EventStore`: no business logic in the request
path (BoD §5.2/§7.6). Side effects, when they exist, are event-stream callbacks
— Phase 0 has none, so the handlers only append events and read history. There
is no agent loop here yet; control frames that drive a loop (confirm/reject/
pause/resume/cancel) are accepted but have no loop to act on in Phase 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Annotated, Literal

import httpx
from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    WSClientFrame,
    WSServerFrame,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import (
    StorageStatus,
    aiter_zip_workspace,
    validate_root,
)
from disco.tools.sandbox._container import USER_PORTS
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from .host_proxy import HostPreviewProxyMiddleware
from .runtime import ConversationRuntime
from .schedule_models import CreateScheduleBody, PreviewScheduleBody

_MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 MB per file
_MAX_FILES_PER_REQUEST = 20
_MAX_CONV_BYTES = 100 * 1024 * 1024    # 100 MB per conversation (uploads/ total)


def _sanitize_name(raw: str) -> str | None:
    """Return a safe filename for uploads/, or None if the result is empty."""
    name = Path(raw).name            # kills traversal (../../etc/passwd → passwd)
    name = unicodedata.normalize("NFC", name)
    name = name.lstrip(".")          # strip leading dots (dotfiles)
    name = re.sub(r"\s+", "-", name.strip())  # strip surrounding whitespace, runs → hyphen
    return name or None


class CreateConversationBody(BaseModel):
    owner_id: str = DEFAULT_OWNER_ID
    space_id: str | None = None
    title: str | None = None
    # "research" (read-only, ungated) | "build" / "agent" (agent + tools + gate;
    # identical machinery, different framing) | "deep_research" (plan→iterate→report).
    # A Literal so a junk surface 422s at the edge rather than persisting a DB label
    # the runtime then coerces to a toolless research loop (the DC-05 half-state).
    surface: Literal["research", "build", "agent", "deep_research"] = "research"
    model_override: str | None = None  # pin the driver model (catalogue key) for this convo
    autonomous: bool = False  # headless/unattended: no ask_user, auto-approve plan, clean forfeit
    # Deep Research depth tier ("quick" | "standard_deep" | "exhaustive"). The UI's
    # depth picker sends it here; the runtime reads it via _depth_for. Without
    # wiring it through, every run silently used the standard_deep default.
    depth_tier: str | None = None


class SendMessageBody(BaseModel):
    content: str


def _user_message(content: str, *, steer: bool = False) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=content),
        meta={"steer": True} if steer else {},
    )


def create_app(store: SqliteEventStore, *, runtime: ConversationRuntime | None = None) -> FastAPI:
    """Build the FastAPI app over a given store. The store is injected so tests
    drive it headlessly. `runtime` runs the agent loop with real inference (Stage
    2); pass None in tests that only exercise the wire layer (the loop won't run)."""
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # On startup, start the MCP pool (RP-05) and reconcile orphaned RUNNING
        # conversations — loops that died with a previous server process. Without
        # this they show 'RUNNING' forever in History / the Deep Research read-only
        # view (and may have leaked a sandbox).
        idle_sweep_task: asyncio.Task | None = None
        schedule_task: asyncio.Task | None = None
        if runtime is not None:
            try:
                await runtime._start_mcp_pool()
            except Exception:
                # D1: _start_mcp_pool handles ApprovalRequired internally;
                # unexpected errors are logged but must not block boot.
                import logging
                _LOG = logging.getLogger(__name__)
                _LOG.warning("MCP pool startup failed", exc_info=True)
            with contextlib.suppress(Exception):  # never block boot on reconciliation
                await runtime.reconcile_orphaned_runs()
            idle_sweep_task = asyncio.create_task(runtime._idle_sweep_loop())
            # RP-08: start the schedule manager loop alongside the idle sweep.
            schedule_task = asyncio.create_task(runtime._schedule_manager_loop())
        yield
        if schedule_task is not None:
            schedule_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await schedule_task
        if idle_sweep_task is not None:
            idle_sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle_sweep_task
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime._close_mcp_pool()

    app = FastAPI(
        title="disco agent-server", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open (ownership is an explicit param, not a cookie)
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    async def _preview_upstream_resolver(cid8: str, port: int) -> str | None:
        """DC-01: {cid8}-{port}.localhost → the conversation's sandbox upstream."""
        if runtime is None:
            return None
        return await runtime.wake_for_preview(cid8, port)

    app.add_middleware(HostPreviewProxyMiddleware, upstream_resolver=_preview_upstream_resolver)

    # ---- health (liveness/readiness — the canary + ops probe hit this) ------

    @app.get("/health")
    async def health() -> JSONResponse:
        """Cheap readiness probe: store reachable + runtime/router wired. Does NOT
        call a model (that's the canary's job — `harness/canary.py` adds a real
        grounded research query on top). Returns 200 ok / 503 degraded so a
        systemd-timer or uptime check can alert on a dead dependency."""
        checks: dict[str, object] = {}
        ok = True
        try:
            await store.list_conversations(owner_id=DEFAULT_OWNER_ID, limit=1)
            checks["store"] = "ok"
        except Exception as e:  # noqa: BLE001 — any store failure is a health signal
            checks["store"] = f"error: {e}"
            ok = False
        if runtime is None:
            checks["runtime"] = "absent (wire-only mode)"
        else:
            try:
                checks["models"] = len(runtime.driver_models().get("models", []))
                checks["runtime"] = "ok"
            except Exception as e:  # noqa: BLE001
                checks["runtime"] = f"error: {e}"
                ok = False
        body = {"status": "ok" if ok else "degraded", "version": app.version, "checks": checks}
        return JSONResponse(body, status_code=200 if ok else 503)

    # ---- MCP server status (RP-05 rung B: per-server health for UI) --------

    @app.get("/api/mcp/servers")
    async def list_mcp_servers() -> dict:
        """Per-server status projection for the UI (rung B).

        Returns the live status of each configured MCP server:
        connected, disconnected, error, disabled, or approval_required.
        HTTP servers reveal their resolved transport info.
        """
        if runtime is None:
            return {"servers": {}}
        cfg = runtime._config_store.load()
        mcp_cfg = cfg.mcp
        if not mcp_cfg.enabled:
            return {"enabled": False, "servers": {}}

        servers: dict[str, dict] = {}
        srv_status = runtime._mcp_pool.server_status() if runtime._mcp_pool else {}
        approval_pending = runtime.mcp_approval_state()

        for name, srv in mcp_cfg.servers.items():
            status = srv_status.get(name, "disconnected")
            # HTTP servers: override status from our own tracking
            if srv.transport == "streamable_http":
                if name in runtime._mcp_http_clients:
                    status = "connected"
                elif name in approval_pending:
                    status = "approval_required"
                else:
                    # Not yet started or failed
                    status = status if status != "disconnected" else "disconnected"

            entry: dict = {
                "name": name,
                "transport": srv.transport,
                "enabled": srv.enabled,
                "status": status,
            }
            if name in approval_pending:
                entry["approval_required"] = True
                entry["description_hash"] = approval_pending[name].get("new_hash", "")
                entry["old_description_hash"] = approval_pending[name].get("old_hash", "")
            else:
                entry["approval_required"] = False

            servers[name] = entry

        return {"enabled": True, "servers": servers}

    @app.get("/api/mcp/servers/{name}/status")
    async def get_mcp_server_status(name: str) -> dict:
        """Per-server health/status endpoint for the UI status projection."""
        if runtime is None:
            return {"name": name, "status": "disconnected", "reason": "no runtime"}
        cfg = runtime._config_store.load()
        srv = cfg.mcp.servers.get(name)
        if srv is None:
            raise HTTPException(status_code=404, detail={"reason": "server_not_found"})

        status = "disconnected"
        if srv.transport == "streamable_http":
            if name in runtime._mcp_http_clients:
                status = "connected"
            else:
                status = "disconnected"
        else:
            if runtime._mcp_pool is not None:
                status = runtime._mcp_pool.server_status().get(name, "disconnected")

        approval_pending = runtime.mcp_approval_state()
        result: dict = {
            "name": name,
            "transport": srv.transport,
            "enabled": srv.enabled,
            "status": status,
        }
        if name in approval_pending:
            result["approval_required"] = True
            result["description_hash"] = approval_pending[name].get("new_hash", "")
            result["old_description_hash"] = approval_pending[name].get("old_hash", "")
        else:
            result["approval_required"] = False

        return result

    # ---- REST surface (§7.5) ------------------------------------------------

    @app.post("/conversations")
    async def create_conversation(body: CreateConversationBody) -> dict:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        store.create_conversation(
            conversation_id,
            owner_id=body.owner_id,
            space_id=body.space_id,
            title=body.title,
            surface=body.surface,  # persist so History routes it (even mid-run, no report yet)
        )
        # Select the surface (Build composes tools + sandbox + the ConfirmRisky gate) +
        # pin the driver model if the picker chose one.
        if runtime is not None:
            runtime.set_surface(conversation_id, body.surface)
            runtime.set_model_override(conversation_id, body.model_override)
            if body.autonomous:
                runtime.set_autonomous(conversation_id, True)
            # Deep Research depth tier (no-op for other surfaces). Was dropped before —
            # every DR run defaulted to standard_deep regardless of the UI picker.
            if body.depth_tier:
                runtime.set_depth(conversation_id, body.depth_tier)
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"/ws/conversations/{conversation_id}",
            "surface": body.surface,
            "sandbox_backend": runtime.sandbox_backend_name() if runtime is not None else None,
        }

    @app.get("/models")
    async def list_models() -> dict:
        """The driver-eligible models for the Build chat model picker (+ the default).
        Sourced from the live router config so it reflects Settings assignments."""
        if runtime is None:
            return {"models": [], "default": None}
        return runtime.driver_models()

    def _reject_if_imported(conversation_id: str) -> None:
        """Imported share-bundle conversations are READ-ONLY — they hold untrusted
        third-party events, so reviving them would feed an attacker's content to the
        agent with this instance's tools/credentials. Every loop-kicking / mutating
        endpoint rejects them at the server EDGE (409); the UI hiding the affordance
        is defense-in-depth, never the boundary."""
        if store.conversation_origin(conversation_id) == "imported":
            raise HTTPException(status_code=409, detail={"reason": "imported_read_only"})

    @app.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: SendMessageBody) -> dict:
        # Append a USER message, then KICK the loop (Stage 2): it runs in the
        # background and streams its events over the conversation's WebSocket.
        _reject_if_imported(conversation_id)
        stored = await store.append(conversation_id, _user_message(body.content))
        if runtime is not None:
            runtime.kick(conversation_id)
        return {"event_id": stored.id, "seq": stored.seq}

    @app.post("/conversations/{conversation_id}/followup")
    async def post_followup(conversation_id: str, body: SendMessageBody) -> dict:
        """Submit a follow-up question on a finished Deep Research report (RP-13).

        Appends the question as a USER message, then kicks the loop. The runtime
        detects a ReportEvent on the conversation and runs a follow-up synthesis
        that reuses the report's passages as grounding."""
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        _reject_if_imported(conversation_id)
        state = await store.get_state(conversation_id)
        # Only accept follow-ups on FINISHED conversations.
        if state.execution_status not in (
            ConversationStatus.FINISHED,
            ConversationStatus.IDLE,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "not_finished",
                    "status": state.execution_status.value,
                },
            )
        stored = await store.append(conversation_id, _user_message(body.content))
        runtime.kick(conversation_id)
        return {"event_id": stored.id, "seq": stored.seq, "followup": True}

    @app.post("/conversations/{conversation_id}/files")
    async def upload_files(
        conversation_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> JSONResponse:
        """Upload files into the conversation's sandbox under uploads/.

        Returns 200 {"saved": [...], "rejected": [...]} unless ALL files are
        rejected (413).  Allowed in every state except terminal ERROR.
        """
        _reject_if_imported(conversation_id)
        state = await store.get_state(conversation_id)
        if state.execution_status == ConversationStatus.ERROR:
            raise HTTPException(status_code=409, detail={"reason": "conversation_in_error_state"})

        if len(files) > _MAX_FILES_PER_REQUEST:
            raise HTTPException(
                status_code=413,
                detail={"reason": f"too_many_files_per_request (max {_MAX_FILES_PER_REQUEST})"},
            )

        if runtime is None:
            raise HTTPException(status_code=409, detail={"reason": "no_active_sandbox"})
        session = runtime.upload_session(conversation_id)

        # Existing uploads/ contents for quota and collision detection.
        # DC-07: account for server-side sidecar uploads as well.
        server_names = runtime.get_upload_names(conversation_id)
        try:
            sandbox_names: set[str] = set(await session.list_dir("uploads"))
        except Exception:  # noqa: BLE001 — uploads/ may not exist yet
            sandbox_names = set()

        existing_names = server_names | sandbox_names

        # Truth for quota is the server-side sidecar, but we check the sandbox for
        # anything that might have been added manually (best effort).
        existing_bytes = runtime.get_upload_size(conversation_id)
        for fname in sandbox_names:
            if fname not in server_names:
                try:
                    existing_bytes += len(await session.read_file(f"uploads/{fname}"))
                except Exception:  # noqa: BLE001
                    pass

        saved: list[dict] = []
        rejected: list[dict] = []
        running_total = existing_bytes

        for upload in files:
            raw_name = upload.filename or ""
            clean = _sanitize_name(raw_name)
            if clean is None:
                rejected.append({"name": raw_name, "reason": "empty filename after sanitization"})
                continue

            data = await upload.read()
            if len(data) > _MAX_FILE_BYTES:
                rejected.append({
                    "name": raw_name,
                    "reason": f"file exceeds 25 MB limit ({len(data):,} bytes)",
                })
                continue

            if running_total + len(data) > _MAX_CONV_BYTES:
                rejected.append({
                    "name": raw_name,
                    "reason": "conversation upload quota (100 MB) would be exceeded",
                })
                continue

            # Collision: suffix -2, -3, …
            stem = Path(clean).stem
            suffix = Path(clean).suffix
            candidate = clean
            counter = 2
            while candidate in existing_names:
                candidate = f"{stem}-{counter}{suffix}"
                counter += 1
            final_name = candidate

            # DC-07: write to sandbox AND server-side storage.
            await session.write_file(f"uploads/{final_name}", data)
            runtime.store_upload(conversation_id, final_name, data)

            existing_names.add(final_name)
            running_total += len(data)
            saved.append({"name": final_name, "bytes": len(data)})

        if saved:
            parts = ", ".join(f"uploads/{s['name']} ({s['bytes']:,} bytes)" for s in saved)
            announcement = f"User uploaded: {parts}"
            await store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=announcement),
                ),
            )

        if not saved and rejected:
            return JSONResponse({"saved": saved, "rejected": rejected}, status_code=413)
        return JSONResponse({"saved": saved, "rejected": rejected}, status_code=200)

    @app.get("/conversations/{conversation_id}/events")
    async def get_events(
        conversation_id: str,
        after_seq: int | None = Query(default=None),
        limit: int = Query(default=100),
    ) -> dict:
        page = await store.paginate(conversation_id, after_seq=after_seq, limit=limit)
        return page.model_dump(mode="json")

    @app.get("/conversations/{conversation_id}/state")
    async def get_state(conversation_id: str) -> dict:
        state = await store.get_state(conversation_id)
        # Same sandbox-liveness overlay as the WS state frame (bp-13): the HTTP
        # surface (agentLive fallback, polling clients, the live specs) must
        # tell the same suspended/active story as the socket.
        if runtime is not None:
            sstate = runtime.sandbox_state(conversation_id)
            if sstate is not None:
                state.extras["sandbox"] = sstate
            # Surface the autonomous flag so the UI can badge the conversation.
            if runtime.is_autonomous(conversation_id):
                state.extras["autonomous"] = True
        result = state.model_dump(mode="json")
        # BP-15: overlay the real sandbox backend name so the UI shows the live tier.
        if runtime is not None:
            sbackend = runtime.sandbox_backend_name()
            if sbackend is not None:
                result["sandbox_backend"] = sbackend
        return result

    @app.get("/conversations/{conversation_id}/preview")
    async def get_preview(conversation_id: str) -> dict:
        """Backend-aware live preview availability (the browser iframes the proxy below)."""
        if runtime is None:
            return {"available": False, "reason": "no runtime"}
        return await runtime.preview(conversation_id)

    @app.post("/conversations/{conversation_id}/preview/restart")
    async def ensure_preview(conversation_id: str) -> dict:
        """Bring a down preview back on demand (§E7) — the UI 'Restart preview' button.
        Bounded + safe (same path as SandboxSession.ensure_preview)."""
        if runtime is None:
            return {"ok": False}
        return {"ok": await runtime.ensure_preview(conversation_id)}

    _MAX_SESSION_TAIL_CHARS = 100_000

    @app.get("/conversations/{conversation_id}/sessions")
    async def list_sessions(conversation_id: str) -> dict:
        """Live tmux session list for the conversation's sandbox (BP-14).
        No sandbox / finished conversation → empty list (200, not 404)."""
        if runtime is None:
            return {"sessions": []}
        sessions, stale = await runtime.sessions_snapshot(conversation_id)
        return {
            "sessions": [
                {
                    "name": s.name,
                    "busy": s.busy,
                    "last_line": next(
                        (line for line in reversed(s.last_lines.splitlines()) if line.strip()),
                        "",
                    ),
                }
                for s in sessions
            ],
            "stale": stale,
        }

    @app.get("/conversations/{conversation_id}/sessions/{name}/view")
    async def get_session_view(
        conversation_id: str,
        name: str,
        tail_chars: int = Query(default=10_000, ge=1, le=_MAX_SESSION_TAIL_CHARS),
    ) -> dict:
        """Live capture-pane tail for one named session (BP-14).
        Internal __-prefixed sessions → 404. Unknown name → 404."""
        if name.startswith("__"):
            raise HTTPException(status_code=404, detail="session not found")
        if runtime is None:
            raise HTTPException(status_code=404, detail="no runtime")
        sessions = await runtime.sessions_list(conversation_id)
        if not any(s.name == name for s in sessions):
            raise HTTPException(status_code=404, detail="session not found")
        view = await runtime.session_view(conversation_id, name, tail_chars)
        if view is None:
            raise HTTPException(status_code=404, detail="no sandbox")
        return {"name": name, "busy": view.running, "content": view.output}

    # ---- Workspace-file route (BP-15) -----------------------------------------------

    _WORKSPACE_PREFIXES = (".pmx/screenshots/", ".pmx/plots/")

    @app.get("/conversations/{conversation_id}/workspace/{path:path}")
    async def workspace_file(conversation_id: str, path: str) -> Response:
        """Serve immutable workspace images (screenshots + plots) from the sandbox.
        Allowlist: .pmx/screenshots/ and .pmx/plots/ ONLY — never user code.
        No sandbox / file absent / path outside allowlist → 404 (never 403)."""
        norm = posixpath.normpath(path)
        if posixpath.isabs(norm) or norm.startswith(".."):
            raise HTTPException(status_code=404)
        if not any(norm.startswith(pfx) for pfx in _WORKSPACE_PREFIXES):
            raise HTTPException(status_code=404)
        if runtime is None:
            raise HTTPException(status_code=404)
        session = runtime.live_session(conversation_id)
        if session is None:
            raise HTTPException(status_code=404)
        try:
            data = await session.read_file(norm)
        except Exception:  # noqa: BLE001 — file absent or sandbox error → 404
            raise HTTPException(status_code=404) from None
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    # ---- Declared-artifact download (rp-11 residue) ---------------------------

    # v1: spreadsheets only. The content-type is pinned to the extension (never
    # sniffed) and served as an ATTACHMENT — we never parse or inline-render the
    # bytes server-side, so an agent overwriting the declared file post-hoc can't
    # turn this into a render/parse exploit.
    _ARTIFACT_TYPES = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    async def _declared_artifacts(conversation_id: str) -> set[str]:
        """The set of workspace-relative paths this conversation EMITTED as results —
        a successful `sheet_generate` observation's filename, or a files-kind
        DeliverableEvent path. The download jail: only emitted artifacts are
        reachable, never arbitrary workspace paths (user code, secrets, uploads)."""
        out: set[str] = set()
        with contextlib.suppress(Exception):
            for e in await store.get_events(conversation_id):
                if (
                    isinstance(e, ObservationEvent)
                    and e.tool_result.success
                    and e.tool_result.tool_name == "sheet_generate"
                    and e.tool_result.structured
                ):
                    fn = e.tool_result.structured.get("filename")
                    if isinstance(fn, str) and fn:
                        out.add(posixpath.normpath(fn))
                elif isinstance(e, DeliverableEvent) and e.artifact_kind == "files":
                    out.add(posixpath.normpath(e.path))
        return out

    @app.get("/conversations/{conversation_id}/artifacts/{path:path}")
    async def artifact_file(conversation_id: str, path: str) -> Response:
        """Download a generated artifact (v1: .xlsx) by its workspace-relative path.
        Jails: (1) the path must have been DECLARED as an artifact in the event log;
        (2) extension allowlist; (3) traversal-normalized + host-path resolve-jail.
        Reads the live sandbox first, falling back to the host ProjectStore snapshot
        so a FINISHED run (no live session) still serves. 404 uniformly (no probe)."""
        norm = posixpath.normpath(path)
        if posixpath.isabs(norm) or norm.startswith(".."):
            raise HTTPException(status_code=404)
        _, ext = posixpath.splitext(norm)
        media_type = _ARTIFACT_TYPES.get(ext.lower())
        if media_type is None:
            raise HTTPException(status_code=404)
        if runtime is None:
            raise HTTPException(status_code=404)
        if norm not in await _declared_artifacts(conversation_id):
            raise HTTPException(status_code=404)

        data: bytes | None = None
        # 1) live sandbox (a running/suspended-but-live conversation)
        session = runtime.live_session(conversation_id)
        if session is not None:
            with contextlib.suppress(Exception):
                data = await session.read_file(norm)
        # 2) host ProjectStore snapshot (finished run, sandbox reaped)
        if data is None:
            ps = runtime.project_store()
            if ps is not None and ps.status() == StorageStatus.OK:
                with contextlib.suppress(Exception):
                    workspace = ps.path_for(conversation_id).resolve()
                    resolved = (workspace / norm).resolve()
                    if resolved.is_relative_to(workspace) and resolved.is_file():
                        data = resolved.read_bytes()
        if data is None:
            raise HTTPException(status_code=404)
        if len(data) > 50 * 1024 * 1024:  # 50 MB cap
            raise HTTPException(status_code=404)

        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{posixpath.basename(norm)}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @app.get("/conversations/{conversation_id}/preview-app/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages
    async def preview_app(conversation_id: str, path: str = "") -> Response:
        """Proxy the agent's dev server through THIS (tailnet-reachable) origin — the
        backend-derived upstream (localhost for local, the remote tailnet IP for gVisor) is
        reached server-side, so no random container port is exposed and previews work over
        the tailnet. Forwards GET; good for a built page (single-origin assets)."""
        upstream = runtime.preview_upstream(conversation_id) if runtime is not None else None
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

    @app.get("/conversations/{conversation_id}/port/{port}/{path:path}")
    @app.get("/conversations/{conversation_id}/port/{port}/")
    # DEPRECATED (DC-01): hostname proxy is canonical; kept one release for single-file pages
    async def port_app(conversation_id: str, port: int, path: str = "") -> Response:
        """Per-port proxy (BP-10): same single-origin forwarding as preview-app for
        the curated USER port set. Arbitrary ints and INTERNAL plumbing ports are
        never proxied (404 — not 503: the port does not exist as a surface)."""
        if port not in USER_PORTS:
            return Response("unknown port", status_code=404, media_type="text/plain")
        upstream = runtime.port_upstream(conversation_id, port) if runtime is not None else None
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

    @app.post("/conversations/{conversation_id}/kill")
    async def kill_conversation(conversation_id: str) -> dict:
        """The KILL SWITCH (BoD §13.6): halt a running agent, tear down its sandbox,
        revoke its capabilities. Always-available; the UI (Prompt 4) wires a button."""
        if runtime is not None:
            await runtime.kill(conversation_id)
        state = await store.get_state(conversation_id)
        return {"killed": True, "state": state.model_dump(mode="json")}

    @app.post("/conversations/{conversation_id}/resume")
    async def post_resume_conversation(conversation_id: str) -> dict:
        """Resume a PAUSED or interrupted-with-unfinished-plan conversation.

        Returns {"ok": true, "status": "RUNNING"} on success.
        Returns 409 {"ok": false, "reason": …} when the transition is illegal
        (RUNNING, FINISHED, ERROR, or any other non-resumable state).
        """
        if runtime is None:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "reason": "runtime_unavailable"},
            )
        _reject_if_imported(conversation_id)
        result = await runtime.resume_conversation(conversation_id)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.get("/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> dict:
        ids = await store.list_conversations(owner_id=owner_id, limit=limit, cursor=cursor)
        return {"conversation_ids": ids}

    # ---- Build projects (persistence list / download / delete) --------------

    @app.get("/api/projects")
    async def list_projects(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
    ) -> dict:
        """List Build projects under the configured projects_root, joined with
        their conversation metadata (title/created_at). Returns an empty list
        with a clear `status` field when the storage isn't configured/valid —
        graceful empty, never crash."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None:
            return {"projects": [], "status": StorageStatus.UNSET.value}
        status = ps.status()
        if status != StorageStatus.OK:
            return {"projects": [], "status": status.value, "root": str(ps.root or "")}
        records = ps.list_projects()
        # cross-reference with conversations so the row title/created_at always
        # come from the authoritative store (manifest can drift on rename).
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        by_id = {s.conversation_id: s for s in summaries}
        projects = []
        for r in records:
            s = by_id.get(r.conversation_id)
            projects.append(
                {
                    "id": r.conversation_id,
                    "owner_id": r.owner_id or (s.owner_id if s else owner_id),
                    "title": (s.title if s else None) or r.title or "(untitled)",
                    # Surface so the Projects list resumes each row on the right
                    # surface ("agent" → /agent/:cid, else /build/:cid). Build-like
                    # surfaces are the only ones that snapshot, so default to "build".
                    "surface": (s.surface if s else None) or "build",
                    "created_at": (s.created_at if s else None) or r.created_at,
                    "last_snapshot_at": r.last_snapshot_at,
                    "file_count": r.file_count,
                    "total_bytes": r.total_bytes,
                    "files_missing": r.files_missing,
                }
            )
        return {"projects": projects, "status": status.value, "root": str(ps.root or "")}

    @app.get("/api/projects/{conversation_id}/download")
    async def download_project(conversation_id: str) -> StreamingResponse:
        """Stream a zip of the project's workspace. 404 with a specific reason
        when the storage is unconfigured / the project is unknown / the files
        have been deleted under the manifest."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(
                status_code=404,
                detail={"reason": "storage_unavailable"},
            )
        record = ps.get(conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
        if record.files_missing:
            raise HTTPException(status_code=404, detail={"reason": "files_missing"})
        workspace = ps.path_for(conversation_id)
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{conversation_id}.zip"'
            ),
        }
        return StreamingResponse(
            aiter_zip_workspace(workspace),
            media_type="application/zip",
            headers=headers,
        )

    @app.get("/api/projects/{conversation_id}/manifest")
    async def project_manifest(conversation_id: str) -> dict:
        """Export a JSON manifest of the project: metadata, the file tree (path +
        bytes), and the agent's last deliverable handoff (title/path/kind +
        deployment_url). The honest, portable description of what the run produced —
        the companion to the workspace zip download."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
        record = ps.get(conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
        # file tree (workspace-relative path + size), skipping the codeact scratch files
        files: list[dict] = []
        workspace = ps.path_for(conversation_id)
        if workspace and workspace.is_dir():
            for p in sorted(workspace.rglob("*")):
                if p.is_file() and not p.name.startswith("_codeact"):
                    files.append(
                        {"path": str(p.relative_to(workspace)), "bytes": p.stat().st_size}
                    )
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

    @app.delete("/api/projects/{conversation_id}")
    async def delete_project(conversation_id: str) -> dict:
        """Remove a project's manifest + workspace from disk. The conversation
        events in SQLite are left alone (deleting those is a separate concern,
        and matches the History surface's existing delete semantics)."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(
                status_code=404,
                detail={"reason": "storage_unavailable"},
            )
        deleted = ps.delete(conversation_id)
        return {"id": conversation_id, "deleted": deleted}

    @app.get("/api/storage/browse")
    async def browse_storage(path: str = Query(default="")) -> dict:
        """Server-side directory picker. Lists IMMEDIATE children of `path` (or
        $HOME when empty). Returns `{path, parent, entries: [{name, is_dir}]}`.
        Only directory listings; never returns file contents — the endpoint
        exists ONLY to drive the settings path picker."""
        try:
            target = Path(path).expanduser().resolve() if path else Path.home()
        except Exception as exc:  # noqa: BLE001 — bad path: 400 with the reason
            raise HTTPException(
                status_code=400, detail={"reason": "bad_path", "message": str(exc)}
            ) from exc
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail={"reason": "not_found", "path": str(target)},
            )
        if not target.is_dir():
            raise HTTPException(
                status_code=400,
                detail={"reason": "not_a_directory", "path": str(target)},
            )
        try:
            entries = []
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                # hide dotfiles — the picker is for project storage, not system browsing
                if child.name.startswith("."):
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                entries.append({"name": child.name, "is_dir": is_dir})
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"reason": "not_readable", "path": str(target)},
            ) from exc
        parent = str(target.parent) if target.parent != target else None
        # Whether the CURRENT path is selectable as a projects_root.
        select_status = validate_root(str(target)).value
        return {
            "path": str(target),
            "parent": parent,
            "entries": entries,
            "selectable": select_status,
        }

    # ---- RP-08: scheduled tasks ----------------------------------------------

    @app.post("/api/conversations/{conversation_id}/schedules")
    async def create_schedule(
        conversation_id: str,
        body: CreateScheduleBody,
    ) -> dict:
        """Create a cron-style recurring schedule for a conversation.

        The cron expression in `rrule` is validated by cronsim; an invalid
        expression returns 422 (never silent — the user must fix it)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        _reject_if_imported(conversation_id)  # a schedule would revive a read-only import
        try:
            result = runtime.create_schedule(
                conversation_id=conversation_id,
                owner_id=DEFAULT_OWNER_ID,
                rrule=body.rrule,
                description=body.description,
                depth=body.depth,
                model_override=body.model_override,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
        return result

    @app.get("/api/conversations/{conversation_id}/schedules")
    async def list_schedules_for_conversation(conversation_id: str) -> dict:
        """List all schedules for a conversation."""
        if runtime is None:
            return {"schedules": []}
        return {
            "schedules": runtime.list_schedules(
                owner_id=DEFAULT_OWNER_ID, conversation_id=conversation_id
            )
        }

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict:
        """Delete a schedule by id. OWNER-SCOPED. Returns `{deleted: true/false}`."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        deleted = runtime.delete_schedule(schedule_id, owner_id=DEFAULT_OWNER_ID)
        return {"ok": deleted, "schedule_id": schedule_id, "deleted": deleted}

    @app.post("/api/schedules/preview")
    async def preview_schedule(body: PreviewScheduleBody) -> dict:
        """Preview the next N run times for a cron expression.  Use this before
        saving a schedule — the confirm card shows next-3-runs to the user."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
        times = runtime.preview_schedule_runs(body.rrule, body.n)
        if not times:
            raise HTTPException(
                status_code=422,
                detail={"reason": f"Invalid or non-firing cron expression: {body.rrule!r}"},
            )
        return {"next_runs": times, "rrule": body.rrule}

    # ---- activity dashboard (running tasks + scheduled-run history) ----------

    @app.get("/api/activity")
    async def get_activity(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        limit: int = Query(default=50),
    ) -> dict:
        """The background-task dashboard feed for one owner:
        - `running`: conversations with a LIVE run task right now, enriched with
          title/status/surface (the runtime is ground truth; cached status can lag).
        - `recent_runs`: recent scheduled-run history (newest first).
        - `counts.running`: the global "N tasks running" indicator value.
        Empty/zeroed (never an error) when there's no runtime or nothing is running."""
        if runtime is None:
            return {"running": [], "recent_runs": [], "counts": {"running": 0}}

        live = runtime.running_conversation_ids()
        running: list[dict] = []
        if live:
            summaries = await store.list_conversation_summaries(
                owner_id=owner_id, limit=500, cursor=None
            )
            by_id = {s.conversation_id: s for s in summaries}
            # owner-scoping IS the security boundary: only running cids that belong to
            # this owner (present in their summaries) are surfaced.
            for cid in live:
                s = by_id.get(cid)
                if s is None:
                    continue
                running.append(
                    {
                        "id": cid,
                        "title": s.title or "(untitled)",
                        "status": s.status,
                        "surface": s.surface,
                        "created_at": s.created_at,
                    }
                )

        recent_runs = runtime.list_recent_schedule_runs(owner_id=owner_id, limit=limit)
        return {
            "running": running,
            "recent_runs": recent_runs,
            "counts": {"running": len(running)},
        }

    # ---- WebSocket (§7.1–7.4) -----------------------------------------------

    @app.websocket("/ws/conversations/{conversation_id}")
    async def conversation_ws(
        websocket: WebSocket,
        conversation_id: str,
        last_seq: int = Query(default=0),
    ) -> None:
        await websocket.accept()
        if runtime is not None:
            runtime.on_connect(conversation_id)

        # (1) On connect: one state snapshot, then replay events after last_seq,
        #     then live — all via the store's subscribe (history-then-live).
        state = await store.get_state(conversation_id)
        # Overlay sandbox liveness so the UI can show "suspended" vs "active" badge.
        if runtime is not None:
            sstate = runtime.sandbox_state(conversation_id)
            if sstate is not None:
                state.extras["sandbox"] = sstate
            if runtime.is_autonomous(conversation_id):
                state.extras["autonomous"] = True
        # BP-15: inject sandbox_backend at the top level of the state dict (same
        # parity as the HTTP /state overlay — the live spec polls HTTP for this).
        state_dict = state.model_dump(mode="json")
        if runtime is not None:
            sbackend = runtime.sandbox_backend_name()
            if sbackend is not None:
                state_dict["sandbox_backend"] = sbackend
        await websocket.send_json({"type": "state", "state": state_dict})
        stream = await store.subscribe(conversation_id, after_seq=last_seq)

        async def pump_events() -> None:
            async for event in stream:
                await websocket.send_json(
                    WSServerFrame(type="event", event=event).model_dump(mode="json")
                )

        # Watch-it-write: drain the EPHEMERAL bus (transient file-stream deltas,
        # never persisted) onto the same socket. A second pump so a flood of
        # stream frames never blocks the primary event pump (and vice versa).
        eph_stream = await store.subscribe_ephemeral(conversation_id)

        async def pump_ephemeral() -> None:
            async for frame in eph_stream:
                # D3: route mcp_approval_required frames via the typed WS event
                if isinstance(frame, dict) and frame.get("type") == "mcp_approval_required":
                    await websocket.send_json(
                        WSServerFrame(
                            type="mcp_approval_required",
                            mcp_approval=frame,
                        ).model_dump(mode="json")
                    )
                else:
                    await websocket.send_json(
                        WSServerFrame(type="file_stream", file_stream=frame).model_dump(mode="json")
                    )

        sender = asyncio.create_task(pump_events())
        eph_sender = asyncio.create_task(pump_ephemeral())
        try:
            while True:
                try:
                    raw = await websocket.receive_json()
                except WebSocketDisconnect:
                    break
                except Exception:  # noqa: BLE001 — non-JSON text frame
                    await websocket.send_json(
                        WSServerFrame(
                            type="error", error={"detail": "malformed frame (not JSON)"}
                        ).model_dump(mode="json")
                    )
                    continue
                try:
                    frame = WSClientFrame.model_validate(raw)
                except ValidationError:
                    await websocket.send_json(
                        WSServerFrame(
                            type="error", error={"detail": "invalid client frame"}
                        ).model_dump(mode="json")
                    )
                    continue
                await _handle_frame(store, websocket, conversation_id, frame, runtime)
        finally:
            sender.cancel()
            eph_sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            with contextlib.suppress(asyncio.CancelledError):
                await eph_sender
            # Last viewer left → after a grace window, free an idle sandbox
            # (an in-flight RUNNING loop is left alone; see runtime._suspend).
            if runtime is not None:
                runtime.on_disconnect(conversation_id)

    # ---- research-answer stream (Stage 4) -----------------------------------

    @app.websocket("/ws/research")
    async def research_ws(websocket: WebSocket) -> None:
        """Live grounded-answer stream. The client sends one `{query, ...}` frame;
        the server streams the research pipeline's frames (state → token… → final
        → state) in the UI's grounded-answer shape, then closes. Read-only: this is
        the retrieval+grounding pipeline, never the agent loop."""
        await websocket.accept()
        if runtime is None:
            await websocket.send_json(
                {"type": "error", "message": "research is not available (no runtime configured)"}
            )
            await websocket.close()
            return
        try:
            raw = await websocket.receive_json()
        except (WebSocketDisconnect, Exception):  # noqa: BLE001 — disconnect/non-JSON
            with contextlib.suppress(Exception):
                await websocket.close()
            return
        body = raw or {}
        query = str(body.get("query", "")).strip()
        if not query:
            await websocket.send_json({"type": "error", "message": "empty query"})
            await websocket.close()
            return
        # Re-scope controls from the UI: the model pill picks the answerer, plus
        # drop-weak and domain-deny.
        model_override = body.get("model_override") or None
        drop_weak = bool(body.get("drop_weak"))
        think = bool(body.get("think"))
        domains = body.get("domains_deny") or []
        domains_deny = frozenset(str(d).strip().lower() for d in domains if str(d).strip())
        try:
            async for frame in runtime.research_stream(
                query,
                model_override=str(model_override) if model_override else None,
                drop_weak=drop_weak,
                domains_deny=domains_deny,
                think=think,
            ):
                await websocket.send_json(frame)
        except WebSocketDisconnect:
            return  # client cancelled mid-stream
        except Exception as exc:  # noqa: BLE001 — surface the real reason, then close
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                )
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    # ---- report export (RP-07) --------------------------------------------

    @app.get("/api/export/capabilities")
    async def export_capabilities_route() -> dict:
        """Which export formats THIS environment can actually produce. md is always
        available; pdf if weasyprint is importable in the agent-server; docx renders
        via pandoc in a transient sandbox, so it needs a CONTAINER backend (the image
        ships pandoc). The UI enables each button from this — never offering a 500."""
        from .report_export import pdf_available

        backend = runtime.sandbox_backend_name() if runtime is not None else None
        # docx renders in a throwaway sandbox container; the process backend (host,
        # no container) can't, so docx is gated on a real container backend.
        docx_ok = backend in ("local", "gvisor", "podman")
        return {"md": True, "pdf": pdf_available(), "docx": docx_ok}

    @app.post("/api/conversations/{conversation_id}/report/export")
    async def export_report(conversation_id: str, fmt: str = Query(...)) -> Response:
        """Export the latest Deep Research report as MD, PDF, or DOCX.

        Query param `fmt` must be md, pdf, or docx.
        Returns 200 with the file streamed (Content-Type + Content-Disposition).
        Returns 404 when no ReportEvent exists for this conversation.
        Returns 400 for an unknown format."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )

        valid_fmts = frozenset({"md", "pdf", "docx"})
        if fmt not in valid_fmts:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown export format: {fmt!r}. Valid: md, pdf, docx",
            )

        try:
            result = await runtime.export_report(
                conversation_id, fmt, owner_id=DEFAULT_OWNER_ID
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": "no_report"},
            )

        payload, media_type, ext = result

        # Build a safe filename from the conversation id
        safe_cid = conversation_id.replace("/", "-").replace("..", "-")
        filename = f"report-{safe_cid}{ext}"

        headers: dict[str, str] = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if media_type:
            headers["Content-Type"] = media_type

        return Response(
            content=payload,
            status_code=200,
            media_type=media_type,
            headers=headers,
        )

    # ---- share links + static viewer (RP-06) -------------------------------

    @app.post("/api/conversations/{conversation_id}/share")
    async def create_share(conversation_id: str) -> dict:
        """Create a revocable share link for a conversation.

        Returns 200 with the token (and its public URL) on success, 404 when
        the conversation does not exist for the default owner. The token is
        base62 (~22 chars / 131 bits) and lives in the `share_tokens` table;
        revocation flips `revoked_at` (a 410 Gone is a probe, so revoked
        tokens look identical to never-issued ones to the viewer)."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )
        result = await runtime.create_share_link_async(
            conversation_id, owner_id=DEFAULT_OWNER_ID
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": result.get("reason", "unknown")},
            )
        return {
            "ok": True,
            "token": result["token"],
            "url": f"/share/{result['token']}",
            "conversation_id": conversation_id,
            "bundle_seq": result["bundle_seq"],
        }

    @app.get("/api/share")
    async def list_share_links() -> dict:
        """List the active share links owned by the default owner. Revoked
        links are filtered out at the store level."""
        if runtime is None:
            return {"links": []}
        return {"links": runtime.list_share_links(owner_id=DEFAULT_OWNER_ID)}

    @app.delete("/api/share/{token}")
    async def revoke_share(token: str) -> dict:
        """Revoke a share link. Returns 200 with `revoked: true/false` (the
        `false` case = token didn't exist, was already revoked, or is not
        owned by the caller). Owner-scoped: a caller can only revoke a
        token it issued (the WHERE clause filters by owner_id)."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )
        ok = runtime.revoke_share_link(token, owner_id=DEFAULT_OWNER_ID)
        return {"ok": ok, "token": token, "revoked": ok}

    @app.get("/api/conversations/{conversation_id}/share/bundle")
    async def export_share_bundle(conversation_id: str) -> dict:
        """Build the scrubbed JSON bundle for a conversation without
        issuing a share link. Useful for direct export (download the
        bundle) and for the reviewer's standalone-rung check. The bundle
        IS the share-viewer payload: same shape, same scrubbing."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )
        result = await runtime.share_export(
            conversation_id, owner_id=DEFAULT_OWNER_ID
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": result.get("reason", "unknown")},
            )
        return result["bundle"]

    @app.post("/api/share/import")
    async def import_share_bundle(bundle: dict) -> dict:
        """Import an exported bundle as a READ-ONLY local conversation. Untrusted
        input → fail-closed validation + re-scrub on ingest + an importer-minted cid
        + an `origin="imported"` marker the read-only guard keys on. 422 (typed
        reason) on any validation failure — never a partial import."""
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        result = await runtime.share_import(bundle, owner_id=DEFAULT_OWNER_ID)
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "reason": result.get("reason", "invalid_bundle")},
            )
        return result

    @app.get("/share/{token}")
    async def share_viewer(token: str) -> HTMLResponse:
        """Serve the read-only static viewer for a shared conversation.

        The page is a single self-contained HTML document (a CDN-free
        stub) that calls `/api/share/{token}/bundle` to fetch the
        scrubbed JSON bundle and renders it with NO WebSocket dependency.
        The viewer code is embedded as a `script` block so the response
        is one round-trip; no external assets are loaded (the CSP
        forbids it).

        404 with NO distinguishing information when the token is missing
        or revoked — the two cases are conflated so a probe cannot
        confirm a token ever existed. The bundle endpoint similarly 404s
        on bad tokens."""
        # Confirmed-revoked vs never-issued are 404 in both cases: the
        # static viewer cannot tell the difference and neither can a
        # probe. The HTML page itself is the same either way.
        return HTMLResponse(_SHARE_VIEWER_HTML)

    @app.get("/api/share/{token}/bundle")
    async def share_bundle(token: str) -> dict:
        """Fetch the scrubbed bundle for a valid share token. The endpoint
        rebuilds the bundle on every call from the live event log (the
        log is append-only; re-export is deterministic). 404 for missing
        or revoked tokens."""
        if runtime is None:
            raise HTTPException(
                status_code=503, detail={"ok": False, "reason": "no_runtime"}
            )
        row = runtime.lookup_share_link(token)
        if row is None:
            # 404 with NO distinguishing detail — revoked and never-issued
            # tokens are intentionally conflated. A probe that varies the
            # token string sees 404 every time.
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "not_found"})
        result = await runtime.share_export(
            row["conversation_id"], owner_id=row["owner_id"]
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": result.get("reason", "unknown")},
            )
        # Surface the bundle_seq the share_tokens row recorded at issue
        # time so the UI can show "this link is N events behind the
        # current run" if the conversation has progressed since then.
        bundle = result["bundle"]
        bundle["share"] = {
            "token": token,
            "owner_id": row["owner_id"],
            "created_at": row["created_at"],
            "bundle_seq_at_issue": row["bundle_seq"],
        }
        return bundle

    return app


# ---- the static share viewer (RP-06) ---------------------------------------

# A minimal, self-contained static viewer. One HTML file, zero external
# assets, no WebSocket. The bundle is fetched from `/api/share/{token}/bundle`
# (same origin; no CORS dance). The page intentionally renders ONLY the
# bundle's `events` + `state` fields — never the WebSocket surface — and
# applies the same buildTrace selectors the live Build surface uses (we
# inline a small, dependency-free version of `deriveActivity` because the
# full `buildTrace` is a 30KB ESM module and we cannot ship a CDN copy).
#
# CSP: no inline-script-unsafe, no external resources, no eval. The whole
# page is one document; the user can save it locally and the bundle survives
# because everything is inlined.
_SHARE_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Shared run — disco</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.45 -apple-system, system-ui, sans-serif;
           max-width: 760px; margin: 2rem auto; padding: 0 1rem;
           color: #222; background: #fafafa; }
    @media (prefers-color-scheme: dark) {
      body { color: #eaeaea; background: #181818; }
    }
    h1 { font-size: 1.1rem; margin: 0 0 0.25rem; font-weight: 600; }
    .meta { color: #777; font-size: 0.85rem; margin-bottom: 1.5rem; }
    .item { border: 1px solid #ddd; border-radius: 6px;
            padding: 0.6rem 0.8rem; margin: 0.4rem 0; background: #fff; }
    @media (prefers-color-scheme: dark) {
      .item { background: #222; border-color: #333; }
    }
    .item.kind-action .label { font-weight: 600; }
    .item.kind-user { background: #f4f7ff; }
    .item.kind-system_warning { background: #fff7e6; }
    .item.kind-agent_message { font-style: italic; }
    .item .thought { white-space: pre-wrap; margin-top: 0.4rem;
                     color: #555; font-size: 0.9rem; }
    .item .detail { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                    font-size: 0.85rem; color: #555; margin-top: 0.25rem;
                    white-space: pre-wrap; word-break: break-word; }
    .err { color: #b00; font-size: 0.85rem; }
    .badge { display: inline-block; padding: 1px 6px; border-radius: 4px;
             font-size: 0.7rem; background: #eee; margin-right: 0.4rem; }
    @media (prefers-color-scheme: dark) {
      .badge { background: #333; }
    }
    .scrubber { position: sticky; top: 0; background: inherit;
                padding: 0.5rem 0; border-bottom: 1px solid #ddd;
                margin-bottom: 1rem; }
    .scrubber input[type=range] { width: 100%; }
    .scrubber .count { font-size: 0.8rem; color: #777; }
  </style>
</head>
<body>
  <div class="scrubber">
    <strong>Replay</strong>
    <span class="count" id="count"></span>
    <input type="range" id="scrub" min="0" max="0" value="0" step="1" />
  </div>
  <h1 id="title">Shared run</h1>
  <div class="meta" id="meta"></div>
  <div id="feed"></div>
  <script>
    // Tiny, dependency-free share viewer. Mirrors the live Build view's
    // "chat + actions" feed shape (user messages, agent prose, tool
    // calls, environment warnings). State and scrubber operate on the
    // BUNDLE — never on a live socket.
    (async () => {
      const params = new URLSearchParams(location.search);
      // The token is the FIRST path segment of the request URL. We could
      // read it server-side and pass it via a script-injected variable,
      // but the page is fully static and the token is the same one the
      // browser hit, so URL parsing is enough.
      const tokenMatch = location.pathname.match(/^\\/share\\/([A-Za-z0-9_-]+)/);
      if (!tokenMatch) {
        document.getElementById('feed').innerHTML =
          '<div class="err">Missing share token in URL.</div>';
        return;
      }
      const token = tokenMatch[1];
      let bundle;
      try {
        const r = await fetch('/api/share/' + encodeURIComponent(token) + '/bundle');
        if (!r.ok) {
          document.getElementById('feed').innerHTML =
            '<div class="err">Share link not found or has been revoked.</div>';
          return;
        }
        bundle = await r.json();
      } catch (e) {
        document.getElementById('feed').innerHTML =
          '<div class="err">Could not load bundle: ' + (e && e.message || e) + '</div>';
        return;
      }

      // Build the scrubber + feed. Pure DOM, no framework.
      const events = (bundle.events || []).slice().sort(
        (a, b) => (a.seq || 0) - (b.seq || 0)
      );
      const scrub = document.getElementById('scrub');
      const count = document.getElementById('count');
      const feed = document.getElementById('feed');
      const titleEl = document.getElementById('title');
      const metaEl = document.getElementById('meta');

      titleEl.textContent = bundle.title || ('Shared run ' + (bundle.conversation_id || ''));
      const metaBits = [
        'surface: ' + (bundle.surface || 'research'),
        'exported: ' + (bundle.exported_at || ''),
        'last_seq: ' + (bundle.last_seq || 0),
        'state: ' + ((bundle.state && bundle.state.execution_status) || '?'),
      ];
      if (bundle.share) {
        metaBits.push('issued: ' + (bundle.share.created_at || ''));
      }
      metaEl.textContent = metaBits.join('  •  ');

      scrub.max = events.length;
      count.textContent = ' step 0 / ' + events.length;

      function escape(s) {
        return String(s || '').replace(/[&<>"']/g, c => (
          { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
      }

      function renderFeed(upto) {
        const slice = events.slice(0, upto);
        if (slice.length === 0) {
          feed.innerHTML = '<div class="meta">No events yet.</div>';
          return;
        }
        const html = slice.map(ev => {
          const k = ev.kind;
          if (k === 'message') {
            const src = ev.source || 'agent';
            const content = (ev.message && ev.message.content) || '';
            if (src === 'user') {
              return '<div class="item kind-user"><span class="badge">user</span>'
                + escape(content) + '</div>';
            } else if (src === 'environment') {
              if (content.startsWith('\u26a0')) {
                return '<div class="item kind-system_warning">'
                  + escape(content) + '</div>';
              }
              return '';
            } else {
              return '<div class="item kind-agent_message">'
                + escape(content) + '</div>';
            }
          } else if (k === 'action') {
            const tc = ev.tool_call || {};
            const args = JSON.stringify(tc.arguments || {});
            return '<div class="item kind-action">'
              + '<span class="badge">action</span>'
              + '<span class="label">' + escape(tc.tool_name || '?') + '</span>'
              + '<div class="detail">' + escape(args) + '</div>'
              + (ev.thought
                  ? '<div class="thought">' + escape(ev.thought) + '</div>'
                  : '')
              + '</div>';
          } else if (k === 'observation') {
            const tr = ev.tool_result || {};
            return '<div class="item"><span class="badge">observation</span>'
              + escape(tr.tool_name || '?') + ': '
              + escape((tr.content || '').slice(0, 200))
              + (tr.content && tr.content.length > 200 ? ' \u2026' : '')
              + '</div>';
          } else if (k === 'agent_error') {
            return '<div class="item"><span class="badge">error</span>'
              + escape(ev.error || '') + '</div>';
          } else if (k === 'plan') {
            return '<div class="item"><span class="badge">plan</span>'
              + escape(ev.summary || '') + '</div>';
          } else if (k === 'status') {
            return '<div class="item"><span class="badge">status</span>'
              + escape(ev.status || '') + '</div>';
          }
          return '';
        }).join('');
        feed.innerHTML = html;
      }

      scrub.addEventListener('input', () => {
        const n = parseInt(scrub.value, 10) || 0;
        count.textContent = ' step ' + n + ' / ' + events.length;
        renderFeed(n);
      });
      renderFeed(events.length);  // start at the end (the "show full" default)
    })();
  </script>
</body>
</html>"""


async def _handle_frame(
    store: SqliteEventStore,
    websocket: WebSocket,
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime | None = None,
) -> None:
    """Map a client frame to the event log. Appended messages are echoed back to
    every subscriber (incl. this socket) as `event` frames — the log is truth.
    A message KICKS the loop (Stage 2) so a real answer streams back."""
    if frame.type == "ping":
        await websocket.send_json(WSServerFrame(type="pong").model_dump(mode="json"))
    elif frame.type in ("send_message", "steer", "confirm", "reject") and (
        store.conversation_origin(conversation_id) == "imported"
    ):
        # Imported (untrusted, read-only) conversations refuse every revive path —
        # the WS is one of them (the easy-to-miss kick site). Refuse, don't kick.
        await websocket.send_json(
            WSServerFrame(type="error", error="imported_read_only").model_dump(mode="json")
        )
    elif frame.type == "send_message" and frame.content is not None:
        await store.append(conversation_id, _user_message(frame.content))
        if runtime is not None:
            runtime.kick(conversation_id)
    elif frame.type == "steer" and frame.steer_text is not None:
        await store.append(conversation_id, _user_message(frame.steer_text, steer=True))
        if runtime is not None:
            runtime.kick(conversation_id)
    elif frame.type == "confirm" and runtime is not None:
        # Approve the pending action: execute exactly it, then resume (Build gate).
        await runtime.confirm(conversation_id)
    elif frame.type == "reject" and runtime is not None:
        # Deny the pending action: record denial, resume without executing.
        await runtime.reject(conversation_id)
    elif frame.type == "approve_plan" and runtime is not None:
        # Approve the pending plan: flip to execution mode and start building.
        await runtime.approve_plan(conversation_id)
    elif frame.type == "request_plan" and runtime is not None:
        # (Re-)enter plan mode with the user's instruction (first plan or re-plan).
        await runtime.request_plan(conversation_id, frame.content or "")
    elif frame.type == "pick_alternative" and runtime is not None and frame.option_id is not None:
        # User chose one of the agent's proposed alternatives (after 4+ failures).
        await runtime.pick_alternative(conversation_id, frame.option_id)
    elif frame.type == "cancel" and runtime is not None:
        # Cooperative stop (the hard kill is POST /conversations/{id}/kill).
        await runtime.cancel(conversation_id)
    elif frame.type == "resume" and runtime is not None:
        # Continue a stopped/incomplete run — re-points at resume_conversation, the
        # same mode-agnostic path the HTTP POST /resume route uses.
        await runtime.resume_conversation(conversation_id)
    # pause: loop-level control, accepted here; wired with the UI later.

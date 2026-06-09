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
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from perpleximanus.core import (
    DEFAULT_OWNER_ID,
    EventSource,
    LLMMessage,
    MessageEvent,
    WSClientFrame,
    WSServerFrame,
)
from perpleximanus.core.store.sqlite import SqliteEventStore
from perpleximanus.tools.projects import (
    StorageStatus,
    aiter_zip_workspace,
    validate_root,
)
from pydantic import BaseModel, ValidationError

from .runtime import ConversationRuntime


class CreateConversationBody(BaseModel):
    owner_id: str = DEFAULT_OWNER_ID
    space_id: str | None = None
    title: str | None = None
    surface: str = "research"  # "research" (read-only, ungated) | "build" (agent + gate)
    model_override: str | None = None  # pin the driver model (catalogue key) for this convo


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
        # On startup, reconcile orphaned RUNNING conversations — loops that died with
        # a previous server process. Without this they show 'RUNNING' forever in
        # History / the Deep Research read-only view (and may have leaked a sandbox).
        if runtime is not None:
            with contextlib.suppress(Exception):  # never block boot on reconciliation
                await runtime.reconcile_orphaned_runs()
        yield

    app = FastAPI(
        title="perpleximanus agent-server", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open (ownership is an explicit param, not a cookie)
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"/ws/conversations/{conversation_id}",
            "surface": body.surface,
        }

    @app.get("/models")
    async def list_models() -> dict:
        """The driver-eligible models for the Build chat model picker (+ the default).
        Sourced from the live router config so it reflects Settings assignments."""
        if runtime is None:
            return {"models": [], "default": None}
        return runtime.driver_models()

    @app.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: SendMessageBody) -> dict:
        # Append a USER message, then KICK the loop (Stage 2): it runs in the
        # background and streams its events over the conversation's WebSocket.
        stored = await store.append(conversation_id, _user_message(body.content))
        if runtime is not None:
            runtime.kick(conversation_id)
        return {"event_id": stored.id, "seq": stored.seq}

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
        return state.model_dump(mode="json")

    @app.get("/conversations/{conversation_id}/preview")
    async def get_preview(conversation_id: str) -> dict:
        """Backend-aware live preview availability (the browser iframes the proxy below)."""
        if runtime is None:
            return {"available": False, "reason": "no runtime"}
        return runtime.preview(conversation_id)

    @app.post("/conversations/{conversation_id}/preview/restart")
    async def restart_preview(conversation_id: str) -> dict:
        """Bring a down preview back on demand (§E7) — the UI 'Restart preview' button.
        Bounded + safe (same path as the agent's restart_preview tool)."""
        if runtime is None:
            return {"ok": False}
        return {"ok": await runtime.restart_preview(conversation_id)}

    @app.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @app.get("/conversations/{conversation_id}/preview-app/")
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

    @app.post("/conversations/{conversation_id}/kill")
    async def kill_conversation(conversation_id: str) -> dict:
        """The KILL SWITCH (BoD §13.6): halt a running agent, tear down its sandbox,
        revoke its capabilities. Always-available; the UI (Prompt 4) wires a button."""
        if runtime is not None:
            await runtime.kill(conversation_id)
        state = await store.get_state(conversation_id)
        return {"killed": True, "state": state.model_dump(mode="json")}

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

    # ---- WebSocket (§7.1–7.4) -----------------------------------------------

    @app.websocket("/ws/conversations/{conversation_id}")
    async def conversation_ws(
        websocket: WebSocket,
        conversation_id: str,
        last_seq: int = Query(default=0),
    ) -> None:
        await websocket.accept()

        # (1) On connect: one state snapshot, then replay events after last_seq,
        #     then live — all via the store's subscribe (history-then-live).
        state = await store.get_state(conversation_id)
        await websocket.send_json(WSServerFrame(type="state", state=state).model_dump(mode="json"))
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

    return app


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
        # Continue a stopped/incomplete run (explicit — never automatic on open).
        # Re-kicks the loop; the deep-research driver re-runs the execution.
        await runtime.resume(conversation_id)
    # pause: loop-level control, accepted here; wired with the UI later.

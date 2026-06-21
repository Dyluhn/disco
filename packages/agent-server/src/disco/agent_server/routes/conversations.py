"""Core conversation lifecycle routes — create/list, message/followup, events/state,
kill/resume, and the workspace-image serve."""

from __future__ import annotations

import posixpath
import uuid

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
)
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query, Response

from ..runtime import ConversationRuntime
from ._common import (
    _WORKSPACE_PREFIXES,
    CreateConversationBody,
    SendMessageBody,
    UpdateSettingsBody,
    _reject_if_imported,
    _user_message,
)


def _resolve_model(body_model_override: str | None, runtime: ConversationRuntime) -> str | None:
    """P3 — resolve the effective driver model for a new conversation.

    Precedence: explicit body.model_override > last-selected (if valid in cfg)
    > None (fall through to RouterConfig.default_model). The cfg guard prevents
    a stale/deleted model key from composing an invalid routing decision."""
    if body_model_override:
        return body_model_override
    last = runtime.get_last_selected_model()
    if last:
        cfg = runtime._config_store.load()
        if last in cfg.models:
            return last
    return None


def make_conversations_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations")
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
            runtime.set_model_override(
                conversation_id, _resolve_model(body.model_override, runtime)
            )
            if body.autonomous:
                runtime.set_autonomous(conversation_id, True)
            # Weak-model assist tier: None ⇒ leave the model-derived default;
            # True/False ⇒ explicit per-conversation override.
            if body.assist is not None:
                runtime.set_assist(conversation_id, body.assist)
            # Deep Research depth tier (no-op for other surfaces). Was dropped before —
            # every DR run defaulted to standard_deep regardless of the UI picker.
            if body.depth_tier:
                runtime.set_depth(conversation_id, body.depth_tier)
            # A4: iterative grounding toggle (no-op for other surfaces). False ⇒
            # leave the default-OFF; True ⇒ enable the re-search/re-check loop.
            if body.iterative:
                runtime.set_iterative(conversation_id, True)
            # DR-3 E2: recency window for time-filtered search + prompt injection.
            if body.recency_window is not None:
                runtime.set_recency(conversation_id, body.recency_window)
            # C6: artifact_mode — NeverConfirm + INTERACTIVE + artifact_scope.
            if body.artifact_mode:
                runtime.set_artifact_mode(conversation_id, True)
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"/ws/conversations/{conversation_id}",
            "surface": body.surface,
            "sandbox_backend": runtime.sandbox_backend_name() if runtime is not None else None,
        }

    @router.patch("/conversations/{conversation_id}/settings")
    async def update_settings(conversation_id: str, body: UpdateSettingsBody) -> dict:
        """runthru-v2 ROOT-1: apply the user's model pick / autonomous / assist choice
        to a PRE-CREATED conversation right before the kick. The build surface
        pre-creates a cid on mount with defaults, then the user picks a model; without
        this the pick was dropped and the run used the default (local Qwen) instead.

        model_override and assist are gated by apply_settings_change (atomic pristine
        check + per-cid lock): if the conversation already has work/run events OR a
        composed loop OR a live run task, the change is REJECTED with 409 and settings
        are left UNMUTATED. autonomous is applied unconditionally (no pristine gate).

        This generalises the prior `cid in _loops` check: the pristine check also
        catches the compose-gap (loop composed but RUNNING not yet emitted) and
        post-restart state (RUNNING/FINISHED in the event store)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        _reject_if_imported(store, conversation_id)

        # Gate model_override + assist together under the atomic pristine check.
        if body.model_override is not None or body.assist is not None:
            resolved_model = (
                _resolve_model(body.model_override, runtime)
                if body.model_override is not None
                else None
            )
            ok = await runtime.apply_settings_change(
                conversation_id,
                model_override=resolved_model,
                assist=body.assist,
            )
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "conversation_not_pristine",
                        "hint": "model/assist settings are fixed once work has begun",
                    },
                )

        if body.autonomous is not None:
            runtime.set_autonomous(conversation_id, body.autonomous)
        return {"ok": True, "model_override": runtime._model_override.get(conversation_id)}

    @router.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: SendMessageBody) -> dict:
        # Append a USER message, then KICK the loop (Stage 2): it runs in the
        # background and streams its events over the conversation's WebSocket.
        _reject_if_imported(store, conversation_id)
        stored = await store.append(conversation_id, _user_message(body.content))
        if runtime is not None:
            runtime.kick(conversation_id)
        return {"event_id": stored.id, "seq": stored.seq}

    @router.post("/conversations/{conversation_id}/followup")
    async def post_followup(conversation_id: str, body: SendMessageBody) -> dict:
        """Submit a follow-up question on a finished Deep Research report (RP-13).

        Appends the question as a USER message, then kicks the loop. The runtime
        detects a ReportEvent on the conversation and runs a follow-up synthesis
        that reuses the report's passages as grounding."""
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        _reject_if_imported(store, conversation_id)
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

    @router.get("/conversations/{conversation_id}/events")
    async def get_events(
        conversation_id: str,
        after_seq: int | None = Query(default=None),
        limit: int = Query(default=100),
    ) -> dict:
        page = await store.paginate(conversation_id, after_seq=after_seq, limit=limit)
        return page.model_dump(mode="json")

    @router.get("/conversations/{conversation_id}/state")
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
            if runtime.is_assist(conversation_id):
                state.extras["assist"] = True
        result = state.model_dump(mode="json")
        # BP-15: overlay the real sandbox backend name so the UI shows the live tier.
        if runtime is not None:
            sbackend = runtime.sandbox_backend_name()
            if sbackend is not None:
                result["sandbox_backend"] = sbackend
        return result

    @router.get("/conversations/{conversation_id}/workspace/{path:path}")
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
        data = None
        if session is not None:
            try:
                data = await session.read_file(norm)
            except Exception:
                pass

        if data is None:
            project_store_method = getattr(runtime, "project_store", None)
            if project_store_method is not None:
                ps = project_store_method()
                if ps is not None:
                    store_path = ps.path_for(conversation_id)
                    if store_path is not None:
                        disk_path = store_path / "snapshot" / norm
                        # Prevent traversal out of snapshot
                        try:
                            disk_path = disk_path.resolve()
                            snap_base = (store_path / "snapshot").resolve()
                            if disk_path.is_file() and disk_path.is_relative_to(snap_base):
                                data = disk_path.read_bytes()
                        except Exception:
                            pass

        if data is None:
            raise HTTPException(status_code=404)
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @router.post("/conversations/{conversation_id}/kill")
    async def kill_conversation(conversation_id: str) -> dict:
        """The KILL SWITCH (BoD §13.6): halt a running agent, tear down its sandbox,
        revoke its capabilities. Always-available; the UI (Prompt 4) wires a button."""
        if runtime is not None:
            await runtime.kill(conversation_id)
        state = await store.get_state(conversation_id)
        return {"killed": True, "state": state.model_dump(mode="json")}

    @router.post("/conversations/{conversation_id}/resume")
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
        _reject_if_imported(store, conversation_id)
        result = await runtime.resume_conversation(conversation_id)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    @router.get("/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> dict:
        ids = await store.list_conversations(owner_id=owner_id, limit=limit, cursor=cursor)
        return {"conversation_ids": ids}

    return router

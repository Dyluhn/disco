"""Core conversation lifecycle routes — create/list, message/followup, events/state,
kill/resume, and the workspace-image serve."""

from __future__ import annotations

import posixpath
import uuid
from dataclasses import asdict

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
)
from disco.core.appkit import classify_build_brief
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException, Query, Response

from ..runtime import (
    ConversationRuntime,
    WorkspaceRestoreConflict,
    WorkspaceRestoreStorageError,
    WorkspaceVersionNotFound,
)
from ..title_service import fallback_title
from ._common import (
    _WORKSPACE_PREFIXES,
    CreateConversationBody,
    SendMessageBody,
    UpdateSettingsBody,
    _build_brief_message,
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
        # BW-09: sanitize a seeded title at the SOURCE so a verbose raw seed (e.g.
        # the Deep Research surface seeding query.slice(0, 100)) is never persisted
        # raw + masked by a CSS truncate at render. Run it through the SAME
        # word-boundary / ~60-char cleaner the auto-titler's fallback uses. None /
        # empty seed → leave it unset so the async auto-titler still owns the title.
        seeded_title = fallback_title(body.title) or None if body.title else None
        store.create_conversation(
            conversation_id,
            owner_id=body.owner_id,
            space_id=body.space_id,
            title=seeded_title,
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
            # EPIC F: appkit_mode — strict phase-based tool allowlist on the build loop.
            if body.appkit_mode:
                runtime.set_appkit_mode(conversation_id, True)
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"/ws/conversations/{conversation_id}",
            "surface": body.surface,
            "sandbox_backend": runtime.sandbox_backend_name() if runtime is not None else None,
        }

    @router.patch("/conversations/{conversation_id}/settings")
    async def update_settings(conversation_id: str, body: UpdateSettingsBody) -> dict:
        """runthru-v2 ROOT-1: apply the user's model pick / autonomous / assist choice.
        The build surface pre-creates a cid on mount with defaults, then the user picks a
        model; without this the pick was dropped and the run used the default (local Qwen).

        STATE-AWARE gate (apply_settings_change, atomic + per-cid lock): a model_override /
        assist change is REJECTED with 409 (settings UNMUTATED) ONLY while a run is
        ACTIVELY in flight — a live run task, RUNNING, or a gate-awaiting state — because
        swapping the brain mid-step is incoherent. It is ALLOWED on a TERMINAL conversation
        (ERROR / STUCK / FINISHED / PAUSED, or IDLE-with-unfinished-plan) so the user can
        change the model before resuming/replanning, AND on a pristine pre-kick IDLE
        conversation (the original flow). On a terminal change the cached loop bound to the
        old model is evicted so the next kick re-resolves the driver with the NEW model.
        autonomous is applied unconditionally (no gate)."""
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        _reject_if_imported(store, conversation_id)

        # Gate model_override + assist together under the atomic state-aware check.
        # PATCH is a PARTIAL update: a model_override field PRESENT (even as null) is an
        # explicit choice — null on a terminal conversation means "reset to default" and
        # must be APPLIED, not dropped (the #24 silent-ignore). A field ABSENT means leave
        # unchanged. `model_fields_set` (Pydantic) is the present-vs-absent signal; the
        # sticky/clear resolution then happens inside apply_settings_change (which knows the
        # terminal-vs-pristine state). No _resolve_model here — sticky-seeding a null is a
        # PRE-KICK convenience owned by apply_settings_change, and applying it on the route
        # would mask an explicit terminal reset.
        model_field_set = "model_override" in body.model_fields_set
        if model_field_set or body.assist is not None:
            ok = await runtime.apply_settings_change(
                conversation_id,
                model_override=body.model_override,
                assist=body.assist,
                model_provided=model_field_set,
            )
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "conversation_not_pristine",
                        "hint": "model/assist settings are fixed while a run is in flight"
                        " — change them once the run has finished, errored, stopped, or paused",
                    },
                )

        if body.autonomous is not None:
            runtime.set_autonomous(conversation_id, body.autonomous)
        return {"ok": True, "model_override": runtime._model_override.get(conversation_id)}

    @router.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: SendMessageBody) -> dict:
        # Append a USER message, then KICK the loop (Stage 2): it runs in the
        # background and streams its events over the conversation's WebSocket.
        # Routed THROUGH the conversation's pinned Build kernel (A1 finding #1):
        # for the default `disco` kernel this is byte-identical to the inline
        # append + kick. (No runtime ⇒ wire-only: append, no kick — unchanged.)
        _reject_if_imported(store, conversation_id)
        brief = classify_build_brief(body.content) if body.build_brief is not None else None
        if runtime is not None:
            stored = await runtime.send_user_turn(
                conversation_id, body.content, build_brief=brief
            )
        else:
            pending = []
            if brief is not None:
                pending.append(_build_brief_message(brief))
            pending.append(_user_message(body.content))
            stored = (await store.append_many(conversation_id, pending))[-1]
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
        # Routed through the pinned Build kernel (A1 finding #1); `runtime` is
        # guaranteed non-None here (checked above). disco kernel ⇒ append + kick.
        stored = await runtime.send_user_turn(conversation_id, body.content)
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
            sandbox_ids = runtime.sandbox_instance_ids(conversation_id)
            if sandbox_ids:
                state.extras["sandbox_instance_ids"] = sandbox_ids
            # Surface the autonomous flag so the UI can badge the conversation.
            if runtime.is_autonomous(conversation_id):
                state.extras["autonomous"] = True
            # ALWAYS emit assist (True or False) — the badge must reflect the CURRENT
            # tier. Emitting it only-when-true let a switch to standard leave the
            # frontend's preserved-on-absent value stuck on "Assist".
            state.extras["assist"] = runtime.is_assist(conversation_id)
        result = state.model_dump(mode="json")
        # BP-15: overlay the real sandbox backend name so the UI shows the live tier.
        if runtime is not None:
            sbackend = runtime.sandbox_backend_name()
            if sbackend is not None:
                result["sandbox_backend"] = sbackend
        # Overlay the stored (auto-titled) conversation title so a resumed surface
        # can render the clean H1 instead of the raw first prompt. None until the
        # async auto-titler lands — the UI falls back to a truncated first task.
        result["title"] = await store.get_title(conversation_id)
        # Overlay the conversation's pinned driver model so a resumed surface seeds its
        # model picker with the ACTUAL current model (not a misleading "default"), and so
        # a terminal-state model swap is verifiable. None ⇒ the conversation runs the
        # router default; the picker then falls back to last-selected/default.
        if runtime is not None:
            result["model_override"] = runtime._model_override.get(conversation_id)
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
        sandbox_ids = runtime.sandbox_instance_ids(conversation_id) if runtime is not None else []
        if runtime is not None:
            await runtime.kill(conversation_id)
        state = await store.get_state(conversation_id)
        return {"killed": True, "state": state.model_dump(mode="json"), "sandbox_instance_ids": sandbox_ids}

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

    @router.get("/conversations/{conversation_id}/versions")
    async def list_workspace_versions(conversation_id: str) -> dict:
        """List saved workspace versions, newest first. Storage problems degrade
        to an empty list like the Projects list instead of breaking the page."""
        if runtime is None:
            return {"versions": []}
        try:
            ps = runtime.project_store()
            if ps is None or ps.status() != StorageStatus.OK:
                return {"versions": []}
            return {"versions": [asdict(v) for v in ps.list_versions(conversation_id)]}
        except Exception:  # noqa: BLE001 — version history is additive/read-only
            return {"versions": []}

    @router.post("/conversations/{conversation_id}/versions/{seq}/restore")
    async def restore_workspace_version(conversation_id: str, seq: int) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "runtime_unavailable"})
        _reject_if_imported(store, conversation_id)
        try:
            return await runtime.restore_workspace_version(conversation_id, seq)
        except WorkspaceRestoreConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": "conversation_running", "message": str(exc)},
            ) from exc
        except WorkspaceVersionNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"reason": "version_not_found", "message": str(exc)},
            ) from exc
        except WorkspaceRestoreStorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "storage_error", "message": str(exc)},
            ) from exc

    @router.get("/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> dict:
        ids = await store.list_conversations(owner_id=owner_id, limit=limit, cursor=cursor)
        return {"conversation_ids": ids}

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
    ) -> dict:
        """Delete a conversation AND release its agent-runtime state (finding #5).

        The app-server library delete removes the DB rows but cannot reach this
        process's per-conversation runtime caches (the kernel pin, cached loop, live
        task, sandbox). It best-effort notifies THIS endpoint so the leak is closed in
        the process that owns the runtime. Runtime cleanup runs FIRST (cancelling the
        live run so its done-callback can't re-pin) and is owner-agnostic — the
        owner-scoped DB delete is the authority on whether the row is actually removed."""
        if runtime is not None:
            await runtime.forget_conversation(conversation_id)
        deleted = await store.delete_conversation(conversation_id, owner_id=owner_id)
        return {"id": conversation_id, "deleted": deleted}

    return router

"""Core conversation lifecycle routes — create/list, message/followup, events/state,
kill/resume, and the workspace-image serve."""

from __future__ import annotations

import logging
import posixpath
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    active_verification_requirements_event,
)
from disco.core.appkit import BuildBrief, classify_build_brief
from disco.core.flags import appkit_enabled
from disco.core.store.base import ConversationSummary
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..auth import current_owner_id, current_session
from ..report_audio import remove_report_audio_cache
from ..runtime import (
    ConversationRuntime,
    WorkspaceRestoreConflict,
    WorkspaceRestoreStorageError,
    WorkspaceVersionNotFound,
)
from ..space_access import owned_space_ids_or_403
from ..space_store import JsonSpaceStore
from ..title_service import fallback_title
from ._common import (
    _WORKSPACE_PREFIXES,
    CreateConversationBody,
    SendMessageBody,
    UpdateSettingsBody,
    _build_brief_message,
    _reject_if_imported,
    _user_message,
    require_owned_conversation,
)

if TYPE_CHECKING:
    from ..host_token_store import HostTokenStore


class ConversationSummaryDTO(BaseModel):
    """Library row the History and Spaces surfaces list."""

    id: str
    owner_id: str
    space_id: str | None = None
    title: str | None = None
    created_at: str
    status: str | None = None
    surface: str = "research"
    origin: str | None = None


class SetConversationSpaceBody(BaseModel):
    space_id: str | None = None


_LOG = logging.getLogger(__name__)
_REQUEST_DEFAULT = cast(Request, None)


def _resolve_model(
    body_model_override: str | None, runtime: ConversationRuntime
) -> tuple[str | None, str | None]:
    """P3 — resolve the effective driver model for a new conversation.

    Precedence: explicit body.model_override > last-selected (if valid in cfg)
    > None (fall through to RouterConfig.default_model). The cfg guard prevents
    a stale/deleted model key from composing an invalid routing decision.

    Returns (model_override, environment_note). The note is populated only when
    a stale last-selected model is ignored so the new conversation can explain
    why it fell back to the default."""
    if body_model_override:
        return body_model_override, None
    last = runtime._settings.get_last_selected_model()
    if last:
        cfg = runtime._config_store.load()
        if last in cfg.models:
            return last, None
        note = (
            f"your previously selected model '{last}' is no longer available; "
            f"using the default '{cfg.default_model}'"
        )
        _LOG.warning(note)
        runtime._settings.set_last_selected_model(None)
        return None, f"⚠ {note}"
    return None, None


async def _owner_for_create_request(request: Request | None) -> str:
    if request is None:
        return DEFAULT_OWNER_ID
    session = current_session(request)
    if session.session_id != "test-session":
        return session.owner_id
    try:
        raw = await request.json()
    except Exception:
        return session.owner_id
    if isinstance(raw, dict) and isinstance(raw.get("owner_id"), str):
        return raw["owner_id"].strip() or session.owner_id
    return session.owner_id


async def _create_conversation_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    body: CreateConversationBody,
    request: Request | None,
) -> dict:
    _validate_create_modes(body)
    conversation_id = f"conv_{uuid.uuid4().hex}"
    session = current_session(request) if request is not None else None
    owner_id = await _owner_for_create_request(request)
    # BW-09: sanitize a seeded title at the SOURCE so a verbose raw seed (e.g.
    # the Deep Research surface seeding query.slice(0, 100)) is never persisted
    # raw + masked by a CSS truncate at render. Run it through the SAME
    # word-boundary / ~60-char cleaner the auto-titler's fallback uses. None /
    # empty seed → leave it unset so the async auto-titler still owns the title.
    seeded_title = fallback_title(body.title) or None if body.title else None
    validated_space_ids = (
        owned_space_ids_or_403(
            runtime,
            body.space_ids,
            owner_id=owner_id,
            include_unclaimed_legacy=bool(session and session.is_admin),
        )
        if body.space_ids
        else frozenset()
    )
    store.create_conversation(
        conversation_id,
        owner_id=owner_id,
        space_id=body.space_id,
        title=seeded_title,
        surface=body.surface,  # persist so History routes it (even mid-run, no report yet)
        appkit_mode=body.appkit_mode,
    )
    # Select surface and pin the driver model if the picker chose one.
    if runtime is not None:
        model_override, model_note = _resolve_model(body.model_override, runtime)
        runtime._settings._set_surface(conversation_id, body.surface)
        runtime._settings.set_model_override(conversation_id, model_override)
        if model_note is not None:
            await store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=model_note),
                ),
            )
        _apply_create_runtime_settings(runtime, conversation_id, body, validated_space_ids)
    return {
        "conversation_id": conversation_id,
        "conversation_url": f"/ws/conversations/{conversation_id}",
        "surface": body.surface,
        "sandbox_backend": (
            runtime._sandbox.backend_name() if runtime is not None else None
        ),
    }


def _validate_create_modes(body: CreateConversationBody) -> None:
    """Refuse mutually-exclusive mode flags and disabled AppKit before any event is persisted."""
    if body.artifact_mode and body.appkit_mode:
        raise HTTPException(
            status_code=409,
            detail="artifact_mode and appkit_mode are mutually exclusive",
        )
    # KILL SWITCH: an explicit appkit_mode request against a deployment that
    # disabled AppKit is refused loudly BEFORE any event is persisted — a silent
    # downgrade to free-form would be a false affordance (the caller asked for
    # the strict allowlist and validated mutators and would not get them).
    if body.appkit_mode and not appkit_enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                "AppKit is disabled on this deployment (DISCO_APPKIT_ENABLED=0). "
                "Create the conversation without appkit_mode to build free-form, "
                "or re-enable the flag and restart disco-agent."
            ),
        )


def _apply_create_runtime_settings(
    runtime: ConversationRuntime,
    conversation_id: str,
    body: CreateConversationBody,
    validated_space_ids: frozenset[str],
) -> None:
    """Apply the per-conversation runtime settings from the create body."""
    if body.autonomous:
        runtime._settings.set_autonomous(conversation_id, True)
    if body.quiet:
        runtime._settings.set_quiet(conversation_id, True)
    # Weak-model assist tier: None ⇒ leave the model-derived default;
    # True/False ⇒ explicit per-conversation override.
    if body.assist is not None:
        runtime._settings.set_assist(conversation_id, body.assist)
    # Deep Research depth tier (no-op for other surfaces). Was dropped before —
    # every DR run defaulted to standard_deep regardless of the UI picker.
    if body.depth_tier:
        runtime._dr.set_depth(conversation_id, body.depth_tier)
    # A4: iterative grounding toggle (no-op for other surfaces). False ⇒
    # leave the default-OFF; True ⇒ enable the re-search/re-check loop.
    if body.iterative:
        runtime._dr.set_iterative(conversation_id, True)
    # DR-3 E2: recency window for time-filtered search + prompt injection.
    if body.recency_window is not None:
        runtime._dr.set_recency(conversation_id, body.recency_window)
    if validated_space_ids:
        runtime._spaces.set_space_ids(conversation_id, validated_space_ids)
    if body.sources:
        runtime._settings.set_research_sources(conversation_id, body.sources)
    # C6: artifact_mode — NeverConfirm + INTERACTIVE + artifact_scope.
    if body.artifact_mode:
        runtime._settings.set_artifact_mode(conversation_id, True)
    # EPIC F: appkit_mode — strict phase-based tool allowlist on the build loop.
    if body.appkit_mode:
        runtime._settings.set_appkit_mode(conversation_id, True)


async def _read_workspace_file(
    runtime: ConversationRuntime, conversation_id: str, norm: str
) -> bytes | None:
    """Read an allowlisted workspace image from the live sandbox, falling back to
    the project-store snapshot on disk. Returns None when no source has the file."""
    session = runtime.live_session(conversation_id)
    if session is not None:
        try:
            data = await session.read_file(norm)
            if data is not None:
                return data
        except Exception:
            pass
    return _read_workspace_snapshot(runtime, conversation_id, norm)


def _read_workspace_snapshot(
    runtime: ConversationRuntime, conversation_id: str, norm: str
) -> bytes | None:
    """Read an allowlisted workspace image from the project-store snapshot on disk."""
    project_store_method = getattr(runtime, "project_store", None)
    if project_store_method is None:
        return None
    ps = project_store_method()
    if ps is None:
        return None
    store_path = ps.path_for(conversation_id)
    if store_path is None:
        return None
    disk_path = store_path / "snapshot" / norm
    # Prevent traversal out of snapshot
    try:
        disk_path = disk_path.resolve()
        snap_base = (store_path / "snapshot").resolve()
        if disk_path.is_file() and disk_path.is_relative_to(snap_base):
            return disk_path.read_bytes()
    except Exception:
        pass
    return None


def _register_workspace_version_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    """Register workspace-version read and restore endpoints."""

    @router.get("/conversations/{conversation_id}/versions")
    async def list_workspace_versions(conversation_id: str, request: Request) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"versions": []}
        try:
            project_store = runtime.project_store()
            if project_store is None or project_store.status() != StorageStatus.OK:
                return {"versions": []}
            return {
                "versions": [
                    asdict(version) for version in project_store.list_versions(conversation_id)
                ]
            }
        except Exception:  # noqa: BLE001 — version history is additive/read-only
            return {"versions": []}

    @router.post("/conversations/{conversation_id}/versions/{seq}/restore")
    async def restore_workspace_version(conversation_id: str, seq: int, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"reason": "runtime_unavailable"})
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        _reject_if_imported(store, conversation_id)
        try:
            return await runtime._workspace.restore_version(conversation_id, seq)
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


async def _apply_gated_compose_settings(runtime, conversation_id: str, body) -> None:
    """Apply the settings that are fixed while a run is in flight, or refuse.

    model_override / assist / the Deep Research compose fields move together
    through the one state-aware gate, because swapping the brain mid-step is
    incoherent. `model_fields_set` is the present-vs-absent signal: a
    model_override present-but-null on a terminal conversation is an explicit
    "reset to default" and must be applied, not silently dropped.
    """
    model_field_set = "model_override" in body.model_fields_set
    deep_fields = {"depth_tier", "iterative", "recency_window", "sources"}
    deep_field_set = bool(deep_fields & body.model_fields_set)
    if not (model_field_set or body.assist is not None or deep_field_set):
        return
    ok = await runtime._settings.apply_settings_change(
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
                "hint": "compose settings are fixed while a run is in flight"
                " — change them once the run has finished, errored, stopped, or paused",
            },
        )


def _apply_ungated_settings(runtime, conversation_id: str, body) -> None:
    """Apply the settings that are safe to change at any point in a run."""
    if body.autonomous is not None:
        runtime._settings.set_autonomous(conversation_id, body.autonomous)
    if body.quiet is not None:
        runtime._settings.set_quiet(conversation_id, body.quiet)
    if "depth_tier" in body.model_fields_set and body.depth_tier is not None:
        runtime._dr.set_depth(conversation_id, body.depth_tier)
    if "iterative" in body.model_fields_set and body.iterative is not None:
        runtime._dr.set_iterative(conversation_id, body.iterative)
    if "recency_window" in body.model_fields_set:
        runtime._dr.set_recency(conversation_id, body.recency_window)
    if "sources" in body.model_fields_set and body.sources is not None:
        runtime._settings.set_research_sources(conversation_id, body.sources)


async def _handle_post_message(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    body: SendMessageBody,
) -> dict:
    """Append a USER message, then KICK the loop (Stage 2).

    Routed THROUGH the conversation's pinned Build kernel (A1 finding #1):
    for the default `disco` kernel this is byte-identical to the inline
    append + kick. (No runtime ⇒ wire-only: append, no kick — unchanged.)
    """
    brief = classify_build_brief(body.content) if body.build_brief is not None else None
    if runtime is not None:
        stored = await runtime.send_user_turn(
            conversation_id,
            body.content,
            build_brief=brief,
            verification_requirements=body.verification_requirements,
        )
    else:
        stored = await _append_wire_only_message(store, conversation_id, body, brief)
    return {"event_id": stored.id, "seq": stored.seq}


async def _append_wire_only_message(
    store: SqliteEventStore,
    conversation_id: str,
    body: SendMessageBody,
    brief: BuildBrief | None,
) -> MessageEvent:
    """Wire-only append (no runtime ⇒ no kick): validate verification requirements
    and append the brief + user message directly to the store."""
    if body.verification_requirements is not None:
        active = active_verification_requirements_event(await store.get_events(conversation_id))
        expected = active.id if active is not None else None
        if body.verification_requirements.supersedes_event_id != expected:
            raise HTTPException(
                status_code=409,
                detail="stale_verification_requirements",
            )
    pending = []
    if brief is not None:
        pending.append(_build_brief_message(brief))
    pending.append(
        _user_message(
            body.content,
            verification_requirements=body.verification_requirements,
        )
    )
    return cast(MessageEvent, (await store.append_many(conversation_id, pending))[-1])


async def _handle_get_state(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
) -> dict:
    """Build the conversation state response with sandbox-liveness and model overlays."""
    state = await store.get_state(conversation_id)
    # Same sandbox-liveness overlay as the WS state frame (bp-13): the HTTP
    # surface (agentLive fallback, polling clients, the live specs) must
    # tell the same suspended/active story as the socket.
    if runtime is not None:
        sstate = runtime._lifecycle.sandbox_state(conversation_id)
        if sstate is not None:
            state.extras["sandbox"] = sstate
        sandbox_ids = runtime._lifecycle.sandbox_instance_ids(conversation_id)
        if sandbox_ids:
            state.extras["sandbox_instance_ids"] = sandbox_ids
        # Surface the autonomous flag so the UI can badge the conversation.
        if runtime._settings.is_autonomous(conversation_id):
            state.extras["autonomous"] = True
        if runtime._settings.is_quiet(conversation_id):
            state.extras["quiet"] = True
        # ALWAYS emit assist (True or False) — the badge must reflect the CURRENT
        # tier. Emitting it only-when-true let a switch to standard leave the
        # frontend's preserved-on-absent value stuck on "Assist".
        state.extras["assist"] = runtime._settings.is_assist(conversation_id)
    result = state.model_dump(mode="json")
    # BP-15: overlay the real sandbox backend name so the UI shows the live tier.
    if runtime is not None:
        sbackend = runtime._sandbox.backend_name()
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
        result["model_override"] = runtime._settings._get_model_override(conversation_id)
    return result


async def _handle_update_settings(
    runtime: ConversationRuntime,
    store: SqliteEventStore,
    conversation_id: str,
    body: UpdateSettingsBody,
    request: Request,
) -> dict:
    """runthru-v2 ROOT-1: apply the user's model pick / autonomous / assist choice.

    STATE-AWARE gate (apply_settings_change, atomic + per-cid lock): a model_override /
    assist change is REJECTED with 409 (settings UNMUTATED) ONLY while a run is
    ACTIVELY in flight. It is ALLOWED on a TERMINAL conversation so the user can
    change the model before resuming/replanning, AND on a pristine pre-kick IDLE
    conversation. autonomous is applied unconditionally (no gate).
    """
    _reject_if_imported(store, conversation_id)
    # Gate model_override + assist + Deep Research compose settings together
    # under the state-aware pristine check. PATCH is a PARTIAL update: a
    # model_override field PRESENT (even as null) is an explicit choice — null
    # on a terminal conversation means "reset to default" and must be APPLIED,
    # not dropped. A field ABSENT means leave unchanged.
    await _apply_gated_compose_settings(runtime, conversation_id, body)
    _apply_ungated_settings(runtime, conversation_id, body)
    return {
        "ok": True,
        "model_override": runtime._settings._get_model_override(conversation_id),
    }


async def _handle_post_followup(
    runtime: ConversationRuntime,
    store: SqliteEventStore,
    conversation_id: str,
    body: SendMessageBody,
) -> dict:
    """Submit a follow-up question on a finished Deep Research report (RP-13)."""
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


async def _handle_kill(
    runtime: ConversationRuntime | None,
    store: SqliteEventStore,
    host_token_store: HostTokenStore | None,
    conversation_id: str,
    request: Request,
) -> dict:
    """The KILL SWITCH (BoD §13.6): halt a running agent, tear down its sandbox,
    revoke its capabilities."""
    _revoke_host_tokens(host_token_store, conversation_id)
    sandbox_ids = (
        runtime._lifecycle.sandbox_instance_ids(conversation_id)
        if runtime is not None
        else []
    )
    if runtime is not None:
        await runtime.kill(conversation_id)
    state = await store.get_state(conversation_id)
    # A successful kill has already revoked host capabilities and destroyed
    # the preview runtime. Keep the DNS-free origin durable across ordinary
    # finish/restart, but release it after this explicit teardown so a long-
    # lived server cannot exhaust the bounded listener pool. If a newer run
    # superseded the kill, ControlOps leaves it non-IDLE and its lease stays.
    if state.execution_status is ConversationStatus.IDLE:
        store.release_local_preview_lease(
            conversation_id=conversation_id,
            owner_id=current_owner_id(request),
        )
    return {
        "killed": True,
        "state": state.model_dump(mode="json"),
        "sandbox_instance_ids": sandbox_ids,
    }


async def _handle_resume(
    runtime: ConversationRuntime,
    store: SqliteEventStore,
    conversation_id: str,
) -> dict:
    """Resume a PAUSED or interrupted-with-unfinished-plan conversation."""
    _reject_if_imported(store, conversation_id)
    await runtime._contract._fold_contract_from_history(conversation_id)
    result = await runtime._resume.resume_conversation(conversation_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result)
    return result


async def _handle_delete(
    runtime: ConversationRuntime | None,
    store: SqliteEventStore,
    host_token_store: HostTokenStore | None,
    conversation_id: str,
    request: Request,
) -> dict:
    """Delete a conversation AND release its agent-runtime state (finding #5).

    Runtime cleanup runs FIRST (cancelling the live run so its done-callback
    can't re-pin) and is owner-agnostic — the owner-scoped DB delete is the
    authority on whether the row is actually removed."""
    owner_id = current_owner_id(request)
    _revoke_host_tokens(host_token_store, conversation_id)
    if runtime is not None:
        await runtime._workspace.forget(conversation_id)
    store.release_local_preview_lease(
        conversation_id=conversation_id,
        owner_id=owner_id,
    )
    deleted = await store.delete_conversation(conversation_id, owner_id=owner_id)
    if deleted:
        remove_report_audio_cache(conversation_id)
    return {"id": conversation_id, "deleted": deleted}


def make_conversations_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    host_token_store: HostTokenStore | None = None,
) -> APIRouter:
    router = APIRouter()
    _register_workspace_version_routes(router, store, runtime)

    @router.post("/conversations")
    async def create_conversation(
        body: CreateConversationBody, request: Request = _REQUEST_DEFAULT
    ) -> dict:
        return await _create_conversation_response(store, runtime, body, request)

    @router.patch("/conversations/{conversation_id}/settings")
    async def update_settings(
        conversation_id: str, body: UpdateSettingsBody, request: Request
    ) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_update_settings(runtime, store, conversation_id, body, request)

    @router.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, body: SendMessageBody, request: Request) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        _reject_if_imported(store, conversation_id)
        return await _handle_post_message(store, runtime, conversation_id, body)

    @router.post("/conversations/{conversation_id}/followup")
    async def post_followup(conversation_id: str, body: SendMessageBody, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not available")
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_post_followup(runtime, store, conversation_id, body)

    @router.get("/conversations/{conversation_id}/events")
    async def get_events(
        conversation_id: str,
        request: Request,
        after_seq: int | None = Query(default=None),
        limit: int = Query(default=100),
    ) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        page = await store.paginate(conversation_id, after_seq=after_seq, limit=limit)
        return page.model_dump(mode="json")

    @router.get("/conversations/{conversation_id}/state")
    async def get_state(conversation_id: str, request: Request) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_get_state(store, runtime, conversation_id)

    @router.get("/conversations/{conversation_id}/workspace/{path:path}")
    async def workspace_file(conversation_id: str, path: str, request: Request) -> Response:
        """Serve immutable workspace images (screenshots + plots) from the sandbox.
        Allowlist: .pmx/screenshots/ and .pmx/plots/ ONLY — never user code.
        No sandbox / file absent / path outside allowlist → 404 (never 403)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        norm = posixpath.normpath(path)
        if posixpath.isabs(norm) or norm.startswith(".."):
            raise HTTPException(status_code=404)
        if not any(norm.startswith(pfx) for pfx in _WORKSPACE_PREFIXES):
            raise HTTPException(status_code=404)
        if runtime is None:
            raise HTTPException(status_code=404)
        data = await _read_workspace_file(runtime, conversation_id, norm)
        if data is None:
            raise HTTPException(status_code=404)
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @router.post("/conversations/{conversation_id}/kill")
    async def kill_conversation(conversation_id: str, request: Request) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_kill(runtime, store, host_token_store, conversation_id, request)

    @router.post("/conversations/{conversation_id}/resume")
    async def post_resume_conversation(conversation_id: str, request: Request) -> dict:
        if runtime is None:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "reason": "runtime_unavailable"},
            )
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_resume(runtime, store, conversation_id)

    @router.get("/conversations")
    async def list_conversations(
        request: Request,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> dict:
        owner_id = current_owner_id(request)
        ids = await store.list_conversations(owner_id=owner_id, limit=limit, cursor=cursor)
        return {"conversation_ids": ids}

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        request: Request,
    ) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        return await _handle_delete(runtime, store, host_token_store, conversation_id, request)

    return router


def _revoke_host_tokens(token_store: HostTokenStore | None, conversation_id: str) -> None:
    if token_store is not None:
        token_store.revoke_for_conversation(conversation_id)


def make_conversation_library_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/conversations")
    async def list_conversation_summaries(
        request: Request,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
        space_id: str | None = Query(default=None),
    ) -> list[ConversationSummaryDTO]:
        owner_id = current_owner_id(request)
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id,
            limit=limit,
            cursor=cursor,
            space_id=space_id,
        )
        return [_conversation_summary_dto(summary) for summary in summaries]

    @router.post("/api/conversations/{conversation_id}/space")
    async def set_conversation_space(
        conversation_id: str,
        body: SetConversationSpaceBody,
        request: Request,
    ) -> dict:
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        session = current_session(request)
        owner_id = session.owner_id
        clean_space_id = body.space_id.strip() if body.space_id else None
        if clean_space_id is not None:
            _get_space_or_404(
                runtime,
                clean_space_id,
                owner_id,
                include_unclaimed_legacy=session.is_admin,
            )
        await store.set_conversation_space(conversation_id, clean_space_id)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "space_id": clean_space_id,
        }

    return router


def _conversation_summary_dto(summary: ConversationSummary) -> ConversationSummaryDTO:
    return ConversationSummaryDTO(
        id=summary.conversation_id,
        owner_id=summary.owner_id,
        space_id=summary.space_id,
        title=summary.title,
        created_at=summary.created_at,
        status=summary.status,
        surface=summary.surface,
        origin=summary.origin,
    )


def _get_space_or_404(
    runtime: ConversationRuntime | None,
    space_id: str,
    owner_id: str,
    *,
    include_unclaimed_legacy: bool = False,
) -> None:
    if runtime is None:
        raise HTTPException(status_code=503, detail={"reason": "no_runtime"})
    project_store = runtime.project_store()
    if project_store.status() != StorageStatus.OK:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "project_storage_unavailable",
                "status": project_store.status().value,
            },
        )
    root = project_store.root
    if root is None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "project_storage_unavailable", "status": "unset"},
        )
    try:
        found = JsonSpaceStore(root).get(
            space_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_space_id", "message": str(exc)},
        ) from exc
    if found is None:
        raise HTTPException(status_code=404, detail={"reason": "space_not_found"})

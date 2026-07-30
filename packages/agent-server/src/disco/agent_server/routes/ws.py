"""WebSocket routes — the conversation event/state stream (§7.1–7.4) and the
read-only research-answer stream (Stage 4)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from disco.core import (
    AgentViewProjection,
    FileStreamFrame,
    WSClientFrame,
    WSServerFrame,
    active_verification_requirements_event,
)
from disco.core.appkit import classify_build_brief
from disco.core.selection_edit import (
    build_scoped_edit_directive,
    parse_selection_ref,
    selection_edit_frame_valid,
)
from disco.core.store.sqlite import SqliteEventStore, SubscriberOverflow
from disco.retrieval.local_encoders import EncoderUnavailable
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..auth import websocket_session
from ..redaction import redact_frame
from ..runtime import ConversationRuntime
from ..space_access import owned_space_ids_or_403
from ._common import (
    _build_brief_message,
    _context_message,
    _user_message,
    validate_canonical_conversation_id,
)


def _file_stream_payload(frame: dict[str, Any]) -> FileStreamFrame:
    """Normalize the internal ephemeral bus envelope to the typed WS payload."""
    inner = frame.get("file_stream")
    payload: dict[str, Any] = inner if isinstance(inner, dict) else frame
    raw_index = payload.get("index", 0)
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        index = 0
    field = payload.get("field")
    return FileStreamFrame(
        tool=str(payload.get("tool") or ""),
        path=str(payload.get("path") or ""),
        index=index,
        delta=str(payload.get("delta") or ""),
        field=field if field in ("content", "new") else "content",
        agent_view_id=(str(payload["agent_view_id"]) if payload.get("agent_view_id") else None),
    )


async def _send_json_redacted(websocket: WebSocket, frame: dict[str, Any]) -> None:
    """The sole outbound JSON seam for both live WebSocket surfaces."""
    await websocket.send_json(redact_frame(frame))


_REVIVE_FRAME_TYPES = frozenset(
    {"send_message", "steer", "inject_source", "confirm", "reject", "selection_edit"}
)


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
        await _send_json_redacted(websocket, WSServerFrame(type="pong").model_dump(mode="json"))
    elif frame.type in _REVIVE_FRAME_TYPES and (
        store.conversation_origin(conversation_id) == "imported"
    ):
        # Imported (untrusted, read-only) conversations refuse every revive path —
        # the WS is one of them (the easy-to-miss kick site). Refuse, don't kick.
        await _send_json_redacted(
            websocket,
            WSServerFrame(type="error", error={"detail": "imported_read_only"}).model_dump(
                mode="json"
            ),
        )
    elif frame.type == "send_message" and frame.content is not None:
        await _handle_send_message_frame(store, websocket, conversation_id, frame, runtime)
    elif frame.type == "steer" and frame.steer_text is not None:
        await _handle_steer_frame(store, conversation_id, frame, runtime)
    elif frame.type == "selection_edit":
        await _handle_selection_edit_frame(store, conversation_id, frame, runtime)
    elif frame.type == "inject_source" and frame.inject_source_text is not None:
        _handle_inject_source_frame(conversation_id, frame, runtime)
    elif runtime is not None:
        await _handle_control_frame(conversation_id, frame, runtime)


async def _handle_control_frame(
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime,
) -> None:
    """Dispatch a control frame (confirm/reject/approve_plan/request_plan/
    pick_alternative/pause/cancel/resume) to the runtime."""
    if frame.type == "confirm":
        await runtime._conversation_control.confirm(conversation_id)
    elif frame.type == "reject":
        await runtime._conversation_control.reject(conversation_id)
    elif frame.type == "approve_plan":
        await runtime._conversation_control.approve_plan(conversation_id)
    elif frame.type == "request_plan":
        await runtime._conversation_control.request_plan(
            conversation_id,
            frame.content or "",
        )
    elif frame.type == "pick_alternative" and frame.option_id is not None:
        await runtime._conversation_control.pick_alternative(
            conversation_id,
            frame.option_id,
        )
    elif frame.type == "pause":
        await runtime._conversation_control.pause(conversation_id)
    elif frame.type == "cancel":
        await runtime.cancel(conversation_id)
    elif frame.type == "resume":
        await runtime._contract._fold_contract_from_history(conversation_id)
        await runtime._resume.resume_conversation(conversation_id)


async def _handle_send_message_frame(
    store: SqliteEventStore,
    websocket: WebSocket,
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime | None,
) -> None:
    """Handle a send_message frame: append context + user message, then kick."""
    content = frame.content or ""
    brief = classify_build_brief(content) if frame.build_brief is not None else None
    # R3: an optional large `context` (e.g. a full DR report) is stored as a
    # HIDDEN ENVIRONMENT message FIRST, then the short visible user message.
    # Routed THROUGH the conversation's pinned Build kernel (A1 finding #1):
    # for the default `disco` kernel this is byte-identical to the inline
    # context+user append + kick. (No runtime ⇒ append only, unchanged.)
    if runtime is not None:
        await runtime.send_user_turn(
            conversation_id,
            content,
            context=frame.context,
            build_brief=brief,
            verification_requirements=frame.verification_requirements,
        )
    else:
        await _append_wire_only_send(store, websocket, conversation_id, frame, brief)


async def _append_wire_only_send(
    store: SqliteEventStore,
    websocket: WebSocket,
    conversation_id: str,
    frame: WSClientFrame,
    brief: Any,
) -> None:
    """Wire-only send (no runtime ⇒ no kick): validate verification requirements
    and append the context + brief + user message directly to the store."""
    if frame.verification_requirements is not None:
        active = active_verification_requirements_event(await store.get_events(conversation_id))
        expected = active.id if active is not None else None
        if frame.verification_requirements.supersedes_event_id != expected:
            await _send_json_redacted(
                websocket,
                WSServerFrame(
                    type="error",
                    error={"detail": "stale_verification_requirements"},
                ).model_dump(mode="json"),
            )
            return
    pending = []
    if frame.context:
        pending.append(_context_message(frame.context))
    if brief is not None:
        pending.append(_build_brief_message(brief))
    pending.append(
        _user_message(
            frame.content or "",
            verification_requirements=frame.verification_requirements,
        )
    )
    await store.append_many(conversation_id, pending)


async def _handle_steer_frame(
    store: SqliteEventStore,
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime | None,
) -> None:
    """Handle a steer frame: route to the DR queue or kick the agent loop."""
    # D3: when a DR run is in flight for this cid, route the steer into the
    # DR queue instead of kicking the agent loop. The engine drains the queue
    # at each section boundary → the steer becomes a new gather leg.
    # OFF-path (no DR run active): falls through to the original agent-loop kick,
    # now routed through the pinned Build kernel (A1 finding #1; disco ⇒ identical).
    steer_text = frame.steer_text or ""
    if runtime is not None and runtime._dr.enqueue_steer(conversation_id, steer_text):
        return
    if runtime is not None:
        await runtime.send_user_turn(conversation_id, steer_text, steer=True)
    else:
        await store.append(conversation_id, _user_message(steer_text, steer=True))


async def _handle_selection_edit_frame(
    store: SqliteEventStore,
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime | None,
) -> None:
    """Handle a selection_edit frame: build a scoped-edit directive and feed it
    as a steer user-turn. A malformed frame is dropped, not kicked."""
    # P8 semantic direct manipulation: the user clicked one preview element and
    # described a change. Build a host-owned, precisely-anchored scoped-edit
    # directive (core owns the truth) and feed it to the loop as a steer user-turn,
    # so the existing targeted-edit law + no-rewrite guards mutate ONLY that element.
    if not selection_edit_frame_valid(frame.selection_ref, frame.edit_instruction):
        return
    ref = parse_selection_ref(frame.selection_ref)
    assert ref is not None and frame.edit_instruction is not None  # validated above
    directive = build_scoped_edit_directive(
        ref, frame.edit_instruction, human_label=frame.human_label
    )
    if runtime is not None:
        await runtime.send_user_turn(conversation_id, directive, steer=True)
    else:
        await store.append(conversation_id, _user_message(directive, steer=True))


def _handle_inject_source_frame(
    conversation_id: str,
    frame: WSClientFrame,
    runtime: ConversationRuntime | None,
) -> None:
    """Handle an inject_source frame: inject a plaintext snippet into the DR corpus."""
    # D3: inject a plaintext snippet into the DR run's corpus mid-run.
    # The text is converted immediately to a Passage so the engine's
    # pop_injected_sources callback can return typed Passage objects.
    # URL → Passage extraction is a follow-up (async, requires extraction
    # provider); only plaintext is accepted in v1.
    if runtime is None:
        return
    import hashlib as _hashlib

    from disco.retrieval.models import Passage as _RetrievalPassage

    text = (frame.inject_source_text or "").strip()
    passage_id = "injected-" + _hashlib.sha256(text.encode()).hexdigest()[:12]
    passage = _RetrievalPassage(
        id=passage_id,
        text=text,
        source_url="user-injected",
        source_title="User-injected source",
    )
    runtime._dr.inject_source(conversation_id, passage)


async def _require_ws_session(websocket: WebSocket):
    session = websocket_session(websocket)
    if session is None:
        await websocket.close(code=1008, reason="auth required")
    return session


async def _require_conversation_ws_session(
    websocket: WebSocket, store: SqliteEventStore, conversation_id: str
):
    session = await _require_ws_session(websocket)
    if session is None:
        return None
    try:
        validate_canonical_conversation_id(conversation_id)
    except HTTPException:
        await websocket.close(code=1008, reason="conversation forbidden")
        return None
    if session.session_id == "test-session":
        return session
    if not await store.conversation_owned_by(conversation_id, session.owner_id):
        await websocket.close(code=1008, reason="conversation forbidden")
        return None
    return session


async def _reject_forbidden_research_conversation(
    websocket: WebSocket,
    store: SqliteEventStore,
    conversation_id: str | None,
    session_id: str,
    owner_id: str,
) -> bool:
    if session_id == "test-session":
        return False
    if conversation_id is not None:
        try:
            validate_canonical_conversation_id(conversation_id)
        except HTTPException:
            await _send_json_redacted(
                websocket, {"type": "error", "message": "conversation forbidden"}
            )
            await websocket.close()
            return True
    if conversation_id is None or await store.conversation_owned_by(conversation_id, owner_id):
        return False
    await _send_json_redacted(websocket, {"type": "error", "message": "conversation forbidden"})
    await websocket.close()
    return True


async def _send_conversation_state_frame(
    store: SqliteEventStore,
    websocket: WebSocket,
    conversation_id: str,
    runtime: ConversationRuntime | None,
) -> None:
    # (1) On connect: one state snapshot, then replay events after last_seq,
    #     then live — all via the store's subscribe (history-then-live).
    state = await store.get_state(conversation_id)
    # Overlay sandbox liveness so the UI can show "suspended" vs "active" badge.
    if runtime is not None:
        sstate = runtime.sandbox_state(conversation_id)
        if sstate is not None:
            state.extras["sandbox"] = sstate
        sandbox_ids = runtime.sandbox_instance_ids(conversation_id)
        if sandbox_ids:
            state.extras["sandbox_instance_ids"] = sandbox_ids
        if runtime.is_autonomous(conversation_id):
            state.extras["autonomous"] = True
        if runtime.is_quiet(conversation_id):
            state.extras["quiet"] = True
        # ALWAYS emit assist (True or False) so the badge reflects the CURRENT
        # tier (only-when-true left a switch-to-standard badge stuck on "Assist").
        state.extras["assist"] = runtime.is_assist(conversation_id)
    # BP-15: mirror the HTTP /state sandbox_backend overlay.
    state_dict = state.model_dump(mode="json")
    if runtime is not None:
        sbackend = runtime.sandbox_backend_name()
        if sbackend is not None:
            state_dict["sandbox_backend"] = sbackend
    await _send_json_redacted(websocket, {"type": "state", "state": state_dict})


async def _pump_conversation_events(
    websocket: WebSocket,
    stream: Any,
    projection: AgentViewProjection | None = None,
) -> None:
    projection = projection or AgentViewProjection()
    try:
        async for event in stream:
            if not projection.accept(event):
                continue
            await _send_json_redacted(
                websocket, WSServerFrame(type="event", event=event).model_dump(mode="json")
            )
    except SubscriberOverflow:
        # Durable history is replayable by sequence. A slow client reconnects
        # instead of retaining an unbounded in-memory queue on the server.
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason="event stream fell behind; reconnect")


async def _pump_ephemeral_frames(websocket: WebSocket, eph_stream: Any) -> None:
    # Watch-it-write: drain the EPHEMERAL bus (transient file-stream deltas,
    # never persisted) onto the same socket. A second pump so a flood of
    # stream frames never blocks the primary event pump (and vice versa).
    async for frame in eph_stream:
        # D3: route mcp_approval_required frames via the typed WS event
        if isinstance(frame, dict) and frame.get("type") == "mcp_approval_required":
            await _send_json_redacted(
                websocket,
                WSServerFrame(
                    type="mcp_approval_required",
                    mcp_approval=frame,
                ).model_dump(mode="json"),
            )
        else:
            await _send_json_redacted(
                websocket,
                WSServerFrame(
                    type="file_stream", file_stream=_file_stream_payload(frame)
                ).model_dump(mode="json"),
            )


async def _run_conversation_ws(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    websocket: WebSocket,
    conversation_id: str,
    last_seq: int,
) -> None:
    """Drive the conversation WebSocket: state frame, event/ephemeral pumps, frame loop."""
    await _send_conversation_state_frame(store, websocket, conversation_id, runtime)
    projection = AgentViewProjection()
    if last_seq > 0:
        for event in await store.get_events(conversation_id):
            if type(event.seq) is int and event.seq <= last_seq:
                projection.accept(event)
    stream = await store.subscribe(conversation_id, after_seq=last_seq)
    eph_stream = await store.subscribe_ephemeral(conversation_id)

    sender = asyncio.create_task(_pump_conversation_events(websocket, stream, projection))
    eph_sender = asyncio.create_task(_pump_ephemeral_frames(websocket, eph_stream))
    try:
        await _ws_frame_loop(store, websocket, conversation_id, runtime)
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


async def _ws_frame_loop(
    store: SqliteEventStore,
    websocket: WebSocket,
    conversation_id: str,
    runtime: ConversationRuntime | None,
) -> None:
    """Receive and dispatch client frames until disconnect."""
    while True:
        try:
            raw = await websocket.receive_json()
        except WebSocketDisconnect:
            break
        except Exception:  # noqa: BLE001 — non-JSON text frame
            await _send_json_redacted(
                websocket,
                WSServerFrame(
                    type="error", error={"detail": "malformed frame (not JSON)"}
                ).model_dump(mode="json"),
            )
            continue
        try:
            frame = WSClientFrame.model_validate(raw)
        except ValidationError:
            await _send_json_redacted(
                websocket,
                WSServerFrame(type="error", error={"detail": "invalid client frame"}).model_dump(
                    mode="json"
                ),
            )
            continue
        await _handle_frame(store, websocket, conversation_id, frame, runtime)


def _parse_research_request(
    body: dict[str, Any], runtime: ConversationRuntime | None
) -> dict[str, Any]:
    """Parse the research request body into the research_stream kwargs."""
    # P3: seed from last-selected when no explicit override is given.
    model_override = body.get("model_override") or None
    if not model_override and runtime is not None:
        model_override = runtime.get_last_selected_model()
    drop_weak = bool(body.get("drop_weak"))
    think = bool(body.get("think"))
    domains = body.get("domains_deny") or []
    domains_deny = frozenset(str(d).strip().lower() for d in domains if str(d).strip())
    # G1/DR-4: optional conversation_id lets the server load seed passages from
    # pre-attached text uploads so they compete in the rerank step alongside
    # live-web content.  None → OFF path (byte-identical to pre-DR-4 code).
    conversation_id = body.get("conversation_id") or None
    if conversation_id is not None:
        conversation_id = str(conversation_id).strip() or None
    raw_sources = body.get("sources") or []
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources = [str(source).strip() for source in raw_sources if str(source).strip()]
    return {
        "model_override": str(model_override) if model_override else None,
        "drop_weak": drop_weak,
        "domains_deny": domains_deny,
        "think": think,
        "conversation_id": conversation_id,
        "sources": sources,
    }


async def _run_research_ws(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    websocket: WebSocket,
    session: Any,
) -> None:
    """Drive the research WebSocket: parse request, validate, stream frames, close."""
    try:
        raw = await websocket.receive_json()
    except (WebSocketDisconnect, Exception):  # noqa: BLE001 — disconnect/non-JSON
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    body = raw or {}
    query = str(body.get("query", "")).strip()
    if not query:
        await _send_json_redacted(websocket, {"type": "error", "message": "empty query"})
        await websocket.close()
        return
    params = _parse_research_request(body, runtime)
    conversation_id = params["conversation_id"]
    if await _reject_forbidden_research_conversation(
        websocket, store, conversation_id, session.session_id, session.owner_id
    ):
        return
    if runtime is None:
        await _send_json_redacted(
            websocket,
            {"type": "error", "message": "research runtime unavailable"},
        )
        await websocket.close()
        return
    raw_space_ids = body.get("space_ids") or []
    if not isinstance(raw_space_ids, list):
        await _send_json_redacted(
            websocket, {"type": "error", "message": "space_ids must be a list"}
        )
        await websocket.close()
        return
    try:
        space_ids = owned_space_ids_or_403(
            runtime,
            raw_space_ids,
            owner_id=session.owner_id,
            include_unclaimed_legacy=session.is_admin,
        )
    except HTTPException as exc:
        await _send_json_redacted(
            websocket, {"type": "error", "message": "space_forbidden", "detail": exc.detail}
        )
        await websocket.close()
        return
    await _stream_research_frames(runtime, websocket, query, params, space_ids, session)


async def _stream_research_frames(
    runtime: ConversationRuntime,
    websocket: WebSocket,
    query: str,
    params: dict[str, Any],
    space_ids: frozenset[str],
    session: Any,
) -> None:
    """Stream research frames to the WebSocket, handling errors and closing."""
    try:
        async for frame in runtime.research_stream(
            query,
            model_override=params["model_override"],
            drop_weak=params["drop_weak"],
            domains_deny=params["domains_deny"],
            think=params["think"],
            conversation_id=params["conversation_id"],
            space_ids=space_ids,
            owner_id=session.owner_id,
            include_unclaimed_legacy=session.is_admin,
            sources=params["sources"],
        ):
            await _send_json_redacted(websocket, frame)
    except WebSocketDisconnect:
        return  # client cancelled mid-stream
    except EncoderUnavailable as exc:
        # RAM guard: in-process encoder couldn't load due to insufficient RAM.
        # Emit an honest, actionable error frame instead of closing the socket
        # silently (which is what an OOM kill / exit 137 would do).
        with contextlib.suppress(Exception):
            await _send_json_redacted(websocket, {"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — surface the real reason, then close
        with contextlib.suppress(Exception):
            await _send_json_redacted(
                websocket, {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            )
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


def make_ws_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/conversations/{conversation_id}")
    async def conversation_ws(
        websocket: WebSocket,
        conversation_id: str,
        # A plain scalar default is intentional. Under the production ASGI
        # stack an omitted WebSocket Query(default=0) could reach the handler as
        # the parameter descriptor rather than integer zero: the state frame
        # was sent, but the history pump never advanced. FastAPI still exposes
        # non-path scalar parameters as query parameters here.
        last_seq: int = 0,
    ) -> None:
        if await _require_conversation_ws_session(websocket, store, conversation_id) is None:
            return
        await websocket.accept()
        if runtime is not None:
            runtime.on_connect(conversation_id)
        await _run_conversation_ws(store, runtime, websocket, conversation_id, last_seq)

    @router.websocket("/ws/research")
    async def research_ws(websocket: WebSocket) -> None:
        """Live grounded-answer stream. The client sends one `{query, ...}` frame;
        the server streams the research pipeline's frames (state → token… → final
        → state) in the UI's grounded-answer shape, then closes. Read-only: this is
        the retrieval+grounding pipeline, never the agent loop."""
        session = await _require_ws_session(websocket)
        if session is None:
            return
        await websocket.accept()
        if runtime is None:
            await _send_json_redacted(
                websocket,
                {"type": "error", "message": "research is not available (no runtime configured)"},
            )
            await websocket.close()
            return
        await _run_research_ws(store, runtime, websocket, session)

    return router

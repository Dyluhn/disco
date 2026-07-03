"""WebSocket routes — the conversation event/state stream (§7.1–7.4) and the
read-only research-answer stream (Stage 4)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from disco.core import (
    WSClientFrame,
    WSServerFrame,
)
from disco.core.appkit import classify_build_brief
from disco.core.selection_edit import (
    build_scoped_edit_directive,
    parse_selection_ref,
    selection_edit_frame_valid,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.local_encoders import EncoderUnavailable
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..runtime import ConversationRuntime
from ._common import _build_brief_message, _context_message, _user_message


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
    elif frame.type in (
        "send_message", "steer", "inject_source", "confirm", "reject", "selection_edit"
    ) and (
        store.conversation_origin(conversation_id) == "imported"
    ):
        # Imported (untrusted, read-only) conversations refuse every revive path —
        # the WS is one of them (the easy-to-miss kick site). Refuse, don't kick.
        # `WSServerFrame.error` is typed `dict[str, Any] | None` but the wire
        # contract here is a free-form reason string — cast to keep the
        # runtime value byte-identical while satisfying the type checker.
        await websocket.send_json(
            WSServerFrame(
                type="error", error=cast("dict[str, Any]", "imported_read_only")
            ).model_dump(mode="json")
        )
    elif frame.type == "send_message" and frame.content is not None:
        brief = classify_build_brief(frame.content) if frame.build_brief is not None else None
        # R3: an optional large `context` (e.g. a full DR report) is stored as a
        # HIDDEN ENVIRONMENT message FIRST, then the short visible user message —
        # so the model receives the report while the history shows only the
        # one-line "Make slides for …" instead of the whole report dumped inline.
        # Routed THROUGH the conversation's pinned Build kernel (A1 finding #1):
        # for the default `disco` kernel this is byte-identical to the inline
        # context+user append + kick. (No runtime ⇒ append only, unchanged.)
        if runtime is not None:
            await runtime.send_user_turn(
                conversation_id, frame.content, context=frame.context, build_brief=brief
            )
        else:
            pending = []
            if frame.context:
                pending.append(_context_message(frame.context))
            if brief is not None:
                pending.append(_build_brief_message(brief))
            pending.append(_user_message(frame.content))
            await store.append_many(conversation_id, pending)
    elif frame.type == "steer" and frame.steer_text is not None:
        # D3: when a DR run is in flight for this cid, route the steer into the
        # DR queue instead of kicking the agent loop. The engine drains the queue
        # at each section boundary → the steer becomes a new gather leg.
        # OFF-path (no DR run active): falls through to the original agent-loop kick,
        # now routed through the pinned Build kernel (A1 finding #1; disco ⇒ identical).
        if runtime is not None and conversation_id in runtime._dr_steer:
            runtime._dr_steer[conversation_id].append(frame.steer_text)
        elif runtime is not None:
            await runtime.send_user_turn(conversation_id, frame.steer_text, steer=True)
        else:
            await store.append(conversation_id, _user_message(frame.steer_text, steer=True))
    elif frame.type == "selection_edit":
        # P8 semantic direct manipulation: the user clicked one preview element and
        # described a change. Build a host-owned, precisely-anchored scoped-edit
        # directive (core owns the truth) and feed it to the loop as a steer user-turn,
        # so the existing targeted-edit law + no-rewrite guards mutate ONLY that element.
        # A malformed frame (bad ref / empty instruction) is dropped, not kicked.
        if selection_edit_frame_valid(frame.selection_ref, frame.edit_instruction):
            ref = parse_selection_ref(frame.selection_ref)
            assert ref is not None and frame.edit_instruction is not None  # validated above
            directive = build_scoped_edit_directive(
                ref, frame.edit_instruction, human_label=frame.human_label
            )
            if runtime is not None:
                await runtime.send_user_turn(conversation_id, directive, steer=True)
            else:
                await store.append(conversation_id, _user_message(directive, steer=True))
    elif frame.type == "inject_source" and frame.inject_source_text is not None:
        # D3: inject a plaintext snippet into the DR run's corpus mid-run.
        # The text is converted immediately to a Passage so the engine's
        # pop_injected_sources callback can return typed Passage objects.
        # URL → Passage extraction is a follow-up (async, requires extraction
        # provider); only plaintext is accepted in v1.
        if runtime is not None and conversation_id in runtime._dr_injected_sources:
            import hashlib as _hashlib

            from disco.retrieval.models import Passage as _RetrievalPassage

            text = frame.inject_source_text.strip()
            passage_id = "injected-" + _hashlib.sha256(text.encode()).hexdigest()[:12]
            passage = _RetrievalPassage(
                id=passage_id,
                text=text,
                source_url="user-injected",
                source_title="User-injected source",
            )
            runtime._dr_injected_sources[conversation_id].append(passage)
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
    elif frame.type == "pause" and runtime is not None:
        # WALK-18 — cooperative pause: sets a flag the loop observes at its next
        # step boundary and lands PAUSED (unlike `cancel`, no lock contention with
        # the in-flight model step). `resume` re-kicks. Hard stop is kill.
        await runtime.pause(conversation_id)
    elif frame.type == "cancel" and runtime is not None:
        # Cooperative stop (the hard kill is POST /conversations/{id}/kill).
        await runtime.cancel(conversation_id)
    elif frame.type == "resume" and runtime is not None:
        # Continue a stopped/incomplete run — re-points at resume_conversation, the
        # same mode-agnostic path the HTTP POST /resume route uses.
        await runtime.resume_conversation(conversation_id)


def make_ws_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/conversations/{conversation_id}")
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
            sandbox_ids = runtime.sandbox_instance_ids(conversation_id)
            if sandbox_ids:
                state.extras["sandbox_instance_ids"] = sandbox_ids
            if runtime.is_autonomous(conversation_id):
                state.extras["autonomous"] = True
            # ALWAYS emit assist (True or False) so the badge reflects the CURRENT
            # tier (only-when-true left a switch-to-standard badge stuck on "Assist").
            state.extras["assist"] = runtime.is_assist(conversation_id)
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

    @router.websocket("/ws/research")
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
        try:
            async for frame in runtime.research_stream(
                query,
                model_override=str(model_override) if model_override else None,
                drop_weak=drop_weak,
                domains_deny=domains_deny,
                think=think,
                conversation_id=conversation_id,
            ):
                await websocket.send_json(frame)
        except WebSocketDisconnect:
            return  # client cancelled mid-stream
        except EncoderUnavailable as exc:
            # RAM guard: in-process encoder couldn't load due to insufficient RAM.
            # Emit an honest, actionable error frame instead of closing the socket
            # silently (which is what an OOM kill / exit 137 would do).
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — surface the real reason, then close
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                )
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    return router

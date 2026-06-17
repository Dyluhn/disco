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
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.local_encoders import EncoderUnavailable
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..runtime import ConversationRuntime
from ._common import _user_message


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
        # `WSServerFrame.error` is typed `dict[str, Any] | None` but the wire
        # contract here is a free-form reason string — cast to keep the
        # runtime value byte-identical while satisfying the type checker.
        await websocket.send_json(
            WSServerFrame(
                type="error", error=cast("dict[str, Any]", "imported_read_only")
            ).model_dump(mode="json")
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
            if runtime.is_autonomous(conversation_id):
                state.extras["autonomous"] = True
            if runtime.is_assist(conversation_id):
                state.extras["assist"] = True
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

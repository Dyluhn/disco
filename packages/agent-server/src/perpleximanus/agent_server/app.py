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

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from perpleximanus.core import (
    DEFAULT_OWNER_ID,
    EventSource,
    LLMMessage,
    MessageEvent,
    WSClientFrame,
    WSServerFrame,
)
from perpleximanus.core.store.sqlite import SqliteEventStore
from pydantic import BaseModel, ValidationError

from .runtime import ConversationRuntime


class CreateConversationBody(BaseModel):
    owner_id: str = DEFAULT_OWNER_ID
    space_id: str | None = None
    title: str | None = None


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
    app = FastAPI(title="perpleximanus agent-server", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open (ownership is an explicit param, not a cookie)
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- REST surface (§7.5) ------------------------------------------------

    @app.post("/conversations")
    async def create_conversation(body: CreateConversationBody) -> dict:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        store.create_conversation(
            conversation_id, owner_id=body.owner_id, space_id=body.space_id, title=body.title
        )
        return {
            "conversation_id": conversation_id,
            "conversation_url": f"/ws/conversations/{conversation_id}",
        }

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

    @app.get("/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> dict:
        ids = await store.list_conversations(owner_id=owner_id, limit=limit, cursor=cursor)
        return {"conversation_ids": ids}

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

        sender = asyncio.create_task(pump_events())
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
            with contextlib.suppress(asyncio.CancelledError):
                await sender

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
        domains = body.get("domains_deny") or []
        domains_deny = frozenset(str(d).strip().lower() for d in domains if str(d).strip())
        try:
            async for frame in runtime.research_stream(
                query,
                model_override=str(model_override) if model_override else None,
                drop_weak=drop_weak,
                domains_deny=domains_deny,
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
    # confirm/reject/pause/resume/cancel: handled by the loop's control ops [VERIFY]
    # — wired further in later stages; accepted here.

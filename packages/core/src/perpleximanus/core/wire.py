"""The streaming & wire contract — event-state-contract.md §7.2/§7.3.

These are the frames exchanged between the agent-server and clients. They are
pure data types (Pydantic) referencing core `Event`/`ConversationState`, with NO
server dependency — so they live in `core` (the contract is core's) and stay
headless-testable. The FastAPI endpoint that *carries* them lives in
`agent-server`. Transport is WebSocket-primary (BoD §4.6).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .events import Event
from .state import ConversationState


class WSServerFrame(BaseModel):
    """[CONTRACT] One server→client frame. Discriminated by `type`."""

    model_config = ConfigDict(frozen=True)
    type: Literal["event", "token", "state", "error", "pong"]
    # type == "event": a newly-appended Event (full object, §2). PRIMARY signal.
    event: Event | None = None
    # type == "token": an incremental token for typewriter rendering. Tokens are
    # NOT persisted as events; the final Action/MessageEvent is the truth.
    # (No model in Phase 0 — present for contract completeness.)
    token: str | None = None
    token_for_event_id: str | None = None
    # type == "state": a ConversationState snapshot (on connect + on change).
    state: ConversationState | None = None
    # type == "error": a transport/protocol error (NOT an agent error).
    error: dict[str, Any] | None = None


class WSClientFrame(BaseModel):
    """[CONTRACT] One client→server frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal[
        "send_message",
        "confirm",
        "reject",
        "steer",
        "pause",
        "resume",
        "cancel",
        "ping",
        # Build plan-mode gate:
        "approve_plan",  # approve the pending plan → start building
        "request_plan",  # (re-)enter plan mode; `content` carries the instruction
    ]
    # send_message / request_plan: free text (a user message / the (re)plan instruction).
    content: str | None = None
    # confirm/reject: respond to WAITING_FOR_CONFIRMATION (echoes pending_action_id).
    action_id: str | None = None
    # steer: redirect a running agent without losing context (BoD §13.4).
    steer_text: str | None = None
    # sent on (re)connect to request replay of events after this seq.
    last_seq: int | None = None

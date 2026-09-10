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

from .appkit import BuildBrief
from .events import Event
from .state import ConversationState
from .verification import VerificationRequirementsDirective


class FileStreamFrame(BaseModel):
    """Payload for a transient watch-it-write frame.

    `delta` appends to the streamed value named by `field`: `content` for
    file_write/file_append-style calls, `new` for file_edit replacement text.
    The final persisted ActionEvent remains authoritative.
    """

    model_config = ConfigDict(frozen=True)
    tool: str
    path: str
    index: int
    delta: str
    field: Literal["content", "new"] = "content"
    agent_view_id: str | None = None


class WSServerFrame(BaseModel):
    """[CONTRACT] One server→client frame. Discriminated by `type`."""

    model_config = ConfigDict(frozen=True)
    type: Literal[
        "event", "token", "file_stream", "state", "error", "pong", "mcp_approval_required"
    ]
    # type == "event": a newly-appended Event (full object, §2). PRIMARY signal.
    # Structured intake is carried here as event.kind == "questions_v2" so replay,
    # reconnect, and normal event-state derivation all stay on the same path.
    event: Event | None = None
    # type == "token": an incremental token for typewriter rendering. Tokens are
    # NOT persisted as events; the final Action/MessageEvent is the truth.
    # (No model in Phase 0 — present for contract completeness.)
    token: str | None = None
    token_for_event_id: str | None = None
    # type == "file_stream": a watch-it-write delta — the driver is assembling a
    # file body/edit replacement in a tool call. NOT persisted; superseded by the
    # final ActionEvent when it lands (which carries the authoritative arguments).
    file_stream: FileStreamFrame | None = None
    # type == "state": a ConversationState snapshot (on connect + on change).
    state: ConversationState | None = None
    # type == "error": a transport/protocol error (NOT an agent error).
    error: dict[str, Any] | None = None
    # type == "mcp_approval_required": MCP server needs re-approval (RP-05 D3).
    # Carries {server, description_hash, old_description_hash}.
    mcp_approval: dict[str, Any] | None = None


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
        # F-3 follow-up: the EXPLICIT "accept the finished build as-is" action.
        # The UI states stop-intent instead of the server guessing it from prose
        # (`control_ops._is_ship_it_intent` stays as the fallback for plain typed
        # messages). Authoritative: the conversation stays FINISHED — no replan,
        # no kick. Optional `content` rides along as a visible user note only.
        "accept_finished",
        # Structured error recovery: pick one of the alternatives the agent
        # proposed after 4 consecutive failures (see AlternativesEvent).
        "pick_alternative",
        # D3 Deep Research mid-run inject-source: fold a user-provided text
        # snippet into the in-flight run's corpus (converted to a Passage by
        # the WS handler). URL extraction is a follow-up; only plaintext is
        # accepted in v1. The `steer` frame type is reused for DR steers
        # (the WS handler routes based on whether a DR run is active).
        "inject_source",
        # P8 semantic direct manipulation: the user clicked one preview element and
        # described a change. `selection_ref` (a SelectionRef mirror) + `edit_instruction`
        # are turned into a host-owned scoped-edit directive (core/selection_edit.py) and
        # fed to the loop as a steer — so the model targets exactly that element.
        "selection_edit",
    ]
    # send_message / request_plan: free text (a user message / the (re)plan instruction).
    # Also carries answers to ask_user / clarify / questions_v2 gates; answering is
    # just the user's next turn, which resumes the loop.
    # accept_finished: an OPTIONAL acknowledgment note echoed to the log — never
    # inspected for intent (the frame itself is the authoritative stop signal).
    content: str | None = None
    # confirm/reject: respond to WAITING_FOR_CONFIRMATION (echoes pending_action_id).
    action_id: str | None = None
    # steer: redirect a running agent without losing context (BoD §13.4).
    # Also routes to DR mid-run steer when a DR run is active (see ws.py).
    steer_text: str | None = None
    # inject_source (D3): plaintext snippet to add to the DR run's corpus.
    inject_source_text: str | None = None
    # sent on (re)connect to request replay of events after this seq.
    last_seq: int | None = None
    # pick_alternative: the id of the AlternativeOption to execute as the next
    # action (the loop pulls its ToolCall and injects it as the resume action).
    option_id: str | None = None
    # send_message (R3): optional LARGE context (e.g. a full DR report) that the
    # MODEL should receive but the USER should NOT see as a giant chat bubble. The
    # WS handler stores it as a hidden EventSource.ENVIRONMENT message BEFORE the
    # short visible user `content`, so the model gets the full report while the
    # history shows only "Make slides for the deep research report: …".
    context: str | None = None
    # send_message (AppKit EPIC B): advisory presence flag. The server recomputes
    # and persists the hidden `<build_brief>` ENVIRONMENT message from content.
    build_brief: BuildBrief | None = None
    # Optional complete proof-requirement snapshot. It can require more proof,
    # never assert that proof exists. Reference pixels remain verifier-only.
    verification_requirements: VerificationRequirementsDirective | None = None
    # selection_edit (P8): the typed ref of the clicked preview element (a SelectionRef
    # mirror — see core/selection_edit.py) and the user's verbatim change instruction.
    selection_ref: dict[str, Any] | None = None
    edit_instruction: str | None = None
    # selection_edit: the selection agent's readable element description
    # (e.g. `h1 — "Nightshift Coffee"`) — helps the model disambiguate the target.
    human_label: str | None = None

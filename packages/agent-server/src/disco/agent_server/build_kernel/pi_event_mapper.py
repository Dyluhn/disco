"""Map sidecar `agent_event` envelopes onto Disco `Event`s (PR I1, pure module).

The Pi sidecar (`packages/pi-kernel/src/events.ts`) projects every Pi
`AgentSessionEvent` into a JSON-safe `MappedAgentEvent` envelope —
`{kind, ...per-kind fields}` — and ships it to the manager wrapped in a
`{"type":"agent_event","event":<envelope>}` protocol frame. Separately, the
sidecar emits protocol-level error frames `{"type":"error","message",fatal?}`
(`runner.ts::error`) for failures that are NOT Pi session events (extension
failures, malformed input, prompt-without-gateway, emit-chain overflow).

`map_pi_event` is the single pure translation seam from BOTH of those frame
shapes onto Disco's append-only `Event` log. The later `pi_kernel.py` wiring
(Batch 3) drains `PiProcess.events()` and feeds each frame here, then appends
the returned events to `runtime._store`. This module is import-leaf and side
effect free: it builds events with the real `disco.core` constructors and never
raises — an unmappable frame yields `[]`.

What this mapper deliberately does NOT do
-----------------------------------------
**It never emits `ActionEvent`/`ObservationEvent` for custom-tool start/end.**
Pi runs with `noTools:"builtin"`, so every tool it calls is a Disco custom tool
that executes through the D2 HTTP tool bridge (`routes/pi_tools.py`). That
bridge is the single source of truth for the Action→Observation pairing: it
appends the `ActionEvent`, executes via `DefaultToolExecutor`, then appends the
correlated `ObservationEvent`/`AgentErrorEvent` (mirroring
`observe.py::execute_and_observe`). If this mapper ALSO emitted Action/
Observation for `tool_execution_start`/`tool_execution_end`, every bridged tool
call would be DOUBLE-appended. So those kinds map to `[]` here by design. (With
`noTools:"builtin"` there are no non-bridged/internal Pi tools; were one ever
introduced, it would need its own non-double-appending handling — it is out of
scope for I1.)

How `agent_end` / finish is represented
---------------------------------------
Disco has no single "finish" event to append. Finishing is a control-flow gate,
not a log entry: the model calls the `finish` virtual tool, the loop intercepts
it and runs the DoD/verifier gate (`loop/engine.py`), and only success drives
the conversation to `FINISHED`. Under PiKernel that `finish` call arrives as a
*custom tool through the D2 bridge* — NOT as the Pi `agent_end` session event.
Pi's `agent_end` merely signals that its internal agent loop returned (it can
even carry `willRetry`). So `agent_end` maps to `[]` here, and the
`pi_kernel.py` wiring handles it separately (drive the finish/verifier request,
revoke the run token, tear the process down) — see PR I2's `finish_request`
span. This keeps the finish decision owned by the bridge + loop, exactly as the
no-double-append rule above demands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from disco.core import (
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
)

# Envelope `kind` values (mirror Pi `AgentSessionEvent.type`; see events.ts).
_ASSISTANT_MESSAGE_KINDS = frozenset({"message_update", "message_end"})
# Bridged-tool lifecycle kinds the D2 bridge already turns into Action/
# Observation — emitting them here too would double-append. See module docstring.
_BRIDGED_TOOL_KINDS = frozenset(
    {"tool_execution_start", "tool_execution_update", "tool_execution_end"}
)
# Error-bearing discriminators. `kind == "extension_error"` is a forward-compat
# guard for an envelope-borne extension failure; `type == "error"` is the actual
# protocol-level error frame the sidecar emits today (runner.ts::error).
_ERROR_DISCRIMINATORS = frozenset({"error", "extension_error"})


def _discriminator(frame: Mapping[str, Any]) -> str:
    """The frame's kind. Prefer the `agent_event` envelope's `kind`; fall back to
    a raw protocol frame's `type` (e.g. the `{"type":"error",...}` error frame)."""
    kind = frame.get("kind")
    if isinstance(kind, str):
        return kind
    type_ = frame.get("type")
    return type_ if isinstance(type_, str) else ""


def _assistant_text(frame: Mapping[str, Any]) -> str | None:
    """Extract assistant text from a `message_update`/`message_end` envelope.

    events.ts carries a `message` summary: `{role, text, parts, content}` (see
    `summarizeMessage`). Returns the text iff this is an assistant message with
    non-empty text; otherwise None (so non-assistant message ends — user echoes,
    tool-result messages — and empty streaming snapshots produce no event)."""
    message = frame.get("message")
    if not isinstance(message, Mapping):
        return None
    if message.get("role") != "assistant":
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text:
        return None
    return text


def _error_message(frame: Mapping[str, Any]) -> str:
    """Best-effort human-readable error string from an error frame/envelope.

    Covers the protocol error frame's `message`, an envelope's `error`, and Pi's
    `errorMessage`/`finalError` (compaction/auto-retry fields). Always returns a
    non-empty string so the AgentErrorEvent is never blank."""
    for key in ("message", "error", "errorMessage", "finalError"):
        value = frame.get(key)
        if isinstance(value, str) and value:
            return value
    return "Pi sidecar reported an error"


def map_pi_event(frame: Mapping[str, Any], *, conversation_id: str) -> list[Event]:
    """Map one sidecar `agent_event` envelope (or protocol error frame) to zero
    or more Disco events.

    Mapping rules (see the module docstring for the rationale):

    - ``message_update`` / ``message_end`` → an assistant ``MessageEvent``
      carrying the running snapshot text. ``meta["pi_streaming"]`` flags an
      in-progress (``message_update``) vs final (``message_end``) snapshot so the
      wiring can de-duplicate/stream as it sees fit. Non-assistant or empty-text
      messages → ``[]``.
    - ``tool_execution_start`` / ``tool_execution_update`` / ``tool_execution_end``
      → ``[]``. The D2 HTTP tool bridge owns the Action/Observation pairing for
      bridged custom tools; emitting here would double-append.
    - an error / extension-failure frame (``type == "error"`` or
      ``kind == "extension_error"``) → an ``AgentErrorEvent``.
    - ``agent_end`` → ``[]``. Finish/verifier is not a single appendable event;
      the ``pi_kernel.py`` wiring drives it (and token revoke + teardown).
    - any unknown/unhandled kind → ``[]`` (never raises).

    `conversation_id` is part of the wiring contract (the caller appends the
    returned events to that conversation's store); it is intentionally not
    stamped onto the events themselves — the store associates events with a
    conversation at append time, not via an event field.
    """
    try:
        kind = _discriminator(frame)

        if kind in _ASSISTANT_MESSAGE_KINDS:
            text = _assistant_text(frame)
            if text is None:
                return []
            return [
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=text),
                    meta={
                        "pi_kind": kind,
                        "pi_streaming": kind == "message_update",
                    },
                )
            ]

        if kind in _ERROR_DISCRIMINATORS:
            return [
                AgentErrorEvent(
                    error=_error_message(frame),
                    meta={"pi_kind": kind},
                )
            ]

        # tool_execution_* (bridge owns Action/Observation), agent_end (finish is
        # driven by the wiring), and every other kind → no appended event.
        return []
    except Exception:
        # Hard guarantee: a malformed frame never breaks the drain loop.
        return []

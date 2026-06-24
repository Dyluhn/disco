"""Event normalizer + deterministic event predicates (guidelines §16).

The classifier consumes Disco event logs as plain JSON dicts — it never imports
`disco.core`. The canonical contract is the FULL persisted event dict (the
`payload` column written by SqliteEventStore, i.e. `event.model_dump(mode="json")`):
a flat dict carrying `id`, `seq`, `kind`, `source`, `timestamp`, plus the
type-specific fields (`message`, `tool_call`, `tool_result`, `action_id`, `status`,
`detail`, `revision`, ...).

`normalize_event` ALSO accepts a row-shaped dict `{seq, kind, source, payload}`
(the shape `scripts/trace_conversation.py` reads off the `events` table) and
flattens it to the canonical full-event dict. So a caller can feed either an
`events.jsonl` of full dicts OR a dump of DB rows and the predicates behave
identically.

All predicates below are PURE and deterministic — same input, same output — so the
adjudicator is reproducible over frozen evidence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

# Canonical event-kind discriminators (mirrors disco.core EventKind values, kept
# as plain strings so the harness has no product dependency).
KIND_MESSAGE = "message"
KIND_ACTION = "action"
KIND_OBSERVATION = "observation"
KIND_AGENT_ERROR = "agent_error"
KIND_CONDENSATION = "condensation"
KIND_STATUS = "status"
KIND_ERROR = "error"
KIND_PLAN = "plan"
KIND_REPORT = "report"
KIND_ALTERNATIVES = "alternatives"
KIND_DELIVERABLE = "deliverable"
KIND_CLARIFY = "clarify"

# Source values.
SRC_USER = "user"
SRC_AGENT = "agent"
SRC_ENVIRONMENT = "environment"
SRC_SYSTEM = "system"

# Terminal conversation statuses (a run "ends" in one of these). Mirrors
# disco.core ConversationStatus values that are terminal-ish for adjudication.
TERMINAL_STATUSES = frozenset({"FINISHED", "ERROR", "IDLE"})

# The status the loop stamps while a proposed plan is halted for the human to
# approve (engine.py _gate_planning_mode, non-autonomous path). It MUST precede a
# RUNNING/plan_approved in an interactive run; autonomous runs skip it (the loop
# auto-approves inline with no human gate).
AWAITING_PLAN_APPROVAL_STATUS = "AWAITING_PLAN_APPROVAL"
# The status detail the loop stamps when a plan is approved (engine.approve_plan /
# the autonomous inline approve). The presence of this detail is the durable
# "approval happened" signal.
PLAN_APPROVED_DETAIL = "plan_approved"
# The status detail enter_planning stamps when (re-)entering planning for a
# request/revision flow. Absent on the very first build turn (which is a plain
# RUNNING with no detail) — see RevisionOracle.
PLANNING_DETAIL = "planning"


class NormalizationError(ValueError):
    """A raw event could not be coerced into the canonical full-event shape."""


# The DB row columns SqliteEventStore writes alongside the JSON `payload` (the
# full event). These are duplicated INSIDE the payload too, so on a row the
# payload is authoritative for content; the columns only fill a gap.
_ROW_COLUMNS = ("seq", "kind", "source", "id", "created_at")


def normalize_event(raw: Any) -> dict[str, Any]:
    """Coerce one raw event into the canonical FULL-event dict.

    Two input shapes, ONE canonical output (same fields regardless of input):

      * the full persisted event dict — `event.model_dump(mode="json")`: a flat
        dict with `kind` and ALL content fields (`tool_call`, `tool_result`,
        `detail`, `revision`, ...) inlined, and NO `payload` member. Returned as a
        shallow copy.

      * a real SQLite row `{seq, kind, source, [id, created_at,] payload}` where
        `payload` is the FULL event — either an already-decoded dict OR (the real
        on-disk shape) a JSON STRING of one. The canonical event is the PARSED
        PAYLOAD (it carries every content field); the top-level row columns only
        FILL a field the payload happens to lack (older dumps). This is the fix
        for the P0 false-negative where a row with top-level `kind` had its payload
        dropped, hiding `detail`/`tool_call`/`revision` from every predicate.

    Raises NormalizationError on anything else (a non-dict, a payload that won't
    parse, or a dict with no discernible kind) so a corrupt log surfaces as
    INVALID_RUN, never a silent mis-parse.
    """
    if not isinstance(raw, dict):
        raise NormalizationError(f"event is not a dict: {type(raw).__name__}")

    # Decode a `payload` member (string -> JSON, or an already-decoded dict).
    parsed_payload: Any = None
    if "payload" in raw:
        payload = raw["payload"]
        if isinstance(payload, str):
            try:
                parsed_payload = json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise NormalizationError(f"row payload is not JSON: {exc}") from exc
        elif isinstance(payload, dict):
            parsed_payload = payload
        else:
            raise NormalizationError(
                f"row payload is neither a JSON string nor a dict: {type(payload).__name__}"
            )

    # Row shape: the payload IS the full event. MERGE — payload content wins, the
    # row's columns only fill a missing field.
    if isinstance(parsed_payload, dict) and "kind" in parsed_payload:
        event = dict(parsed_payload)
        for col in _ROW_COLUMNS:
            if event.get(col) is None and raw.get(col) is not None:
                event[col] = raw[col]
        return event

    # Full event dict (no payload member, kind inlined).
    if "kind" in raw and "payload" not in raw:
        return dict(raw)

    raise NormalizationError(
        f"event has no discernible kind (and no usable payload): keys={sorted(raw)}"
    )


def normalize_events(raw_events: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize a sequence of raw events and return them ordered by `seq`.

    Events whose `seq` is None sort after numbered ones in their original order
    (a not-yet-persisted event should never appear in durable evidence, but we
    never crash on it — durability is the harness-validity oracle's call)."""
    normalized = [normalize_event(e) for e in raw_events]

    def _key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        idx, ev = item
        seq = ev.get("seq")
        if isinstance(seq, int):
            return (0, seq)
        return (1, idx)

    return [ev for _, ev in sorted(enumerate(normalized), key=_key)]


# ---- field accessors --------------------------------------------------------


def kind_of(ev: dict[str, Any]) -> str:
    return str(ev.get("kind"))


def seq_of(ev: dict[str, Any]) -> int:
    seq = ev.get("seq")
    return seq if isinstance(seq, int) else -1


def source_of(ev: dict[str, Any]) -> str:
    return str(ev.get("source"))


def tool_name_of(ev: dict[str, Any]) -> str | None:
    """The tool name an ACTION event proposes, else None."""
    if kind_of(ev) != KIND_ACTION:
        return None
    tc = ev.get("tool_call") or {}
    name = tc.get("tool_name")
    return str(name) if name else None


def message_role_of(ev: dict[str, Any]) -> str | None:
    if kind_of(ev) != KIND_MESSAGE:
        return None
    msg = ev.get("message") or {}
    role = msg.get("role")
    return str(role) if role else None


# ---- the §16 event predicates ----------------------------------------------


def has_user_message(events: list[dict[str, Any]], *, after_seq: int = 0) -> bool:
    """True when a USER-sourced message event exists with seq > after_seq.

    A user message is `kind == "message"` AND `source == "user"`. (The message's
    own role is also "user", but the event source is the durable provenance signal
    the security/UI layers use, so we key on it — §1.6.)"""
    return any(
        kind_of(e) == KIND_MESSAGE
        and source_of(e) == SRC_USER
        and seq_of(e) > after_seq
        for e in events
    )


def has_plan(
    events: list[dict[str, Any]], *, revision: int | None = None, after_seq: int = 0
) -> bool:
    """True when a PlanEvent exists with seq > after_seq (and matching `revision`
    when given)."""
    for e in events:
        if kind_of(e) != KIND_PLAN or seq_of(e) <= after_seq:
            continue
        if revision is not None and int(e.get("revision", 1)) != revision:
            continue
        return True
    return False


def has_status(
    events: list[dict[str, Any]],
    *,
    detail: str | None = None,
    status: str | None = None,
    after_seq: int = 0,
) -> bool:
    """True when a StatusEvent exists matching the given `status` and/or `detail`,
    with seq > after_seq."""
    for e in events:
        if kind_of(e) != KIND_STATUS or seq_of(e) <= after_seq:
            continue
        if status is not None and str(e.get("status")) != status:
            continue
        if detail is not None and (e.get("detail") or None) != detail:
            continue
        return True
    return False


def has_action(
    events: list[dict[str, Any]], *, tool_name: str | None = None, after_seq: int = 0
) -> bool:
    """True when an ActionEvent exists (optionally for `tool_name`) with seq >
    after_seq."""
    for e in events:
        if kind_of(e) != KIND_ACTION or seq_of(e) <= after_seq:
            continue
        if tool_name is not None and tool_name_of(e) != tool_name:
            continue
        return True
    return False


def has_observation_for_action(events: list[dict[str, Any]], action_id: str) -> bool:
    """True when an OBSERVATION or AGENT_ERROR event references `action_id`.

    Per the EventChain caveat: a tool rejection / error is recorded as an
    AgentErrorEvent (`action_id == X`) and that IS visible feedback to the model —
    a VALID pairing for the action, distinct from a genuinely-missing observation.
    """
    for e in events:
        k = kind_of(e)
        if k == KIND_OBSERVATION and str(e.get("action_id")) == action_id:
            return True
        if k == KIND_AGENT_ERROR and e.get("action_id") is not None and (
            str(e.get("action_id")) == action_id
        ):
            return True
    return False


def has_tool_rejection(events: list[dict[str, Any]]) -> bool:
    """True when any tool call was rejected — an AgentErrorEvent, or an
    ObservationEvent whose tool_result.success is False."""
    for e in events:
        k = kind_of(e)
        if k == KIND_AGENT_ERROR:
            return True
        if k == KIND_OBSERVATION:
            tr = e.get("tool_result") or {}
            if tr.get("success") is False:
                return True
    return False


def terminal_status(events: list[dict[str, Any]]) -> str | None:
    """The last terminal status the run reached (FINISHED/ERROR/IDLE), else None
    (the run never reached a terminal state — STUCK_RUNNING territory)."""
    result: str | None = None
    for e in events:
        if kind_of(e) != KIND_STATUS:
            continue
        st = str(e.get("status"))
        if st in TERMINAL_STATUSES:
            result = st
        elif st == "RUNNING":
            # Re-entering RUNNING after a terminal means the run reopened
            # (a follow-up / replan) — clear the terminal marker.
            result = None
    return result


def latest_plan_revision(events: list[dict[str, Any]], *, after_seq: int = 0) -> int:
    """The highest plan revision seen with seq > after_seq, or 0 when no plan
    exists. Mirrors plans.py's semantics: the loop assigns
    `revision = 1 + (count of prior PlanEvents)`, so the latest PlanEvent's stored
    `revision` is also the count of plans so far — both views agree."""
    best = 0
    for e in events:
        if kind_of(e) != KIND_PLAN or seq_of(e) <= after_seq:
            continue
        best = max(best, int(e.get("revision", 1)))
    return best


def plan_event_count(events: list[dict[str, Any]], *, after_seq: int = 0) -> int:
    return sum(
        1 for e in events if kind_of(e) == KIND_PLAN and seq_of(e) > after_seq
    )


def first_user_message_seq(events: list[dict[str, Any]]) -> int | None:
    for e in events:
        if kind_of(e) == KIND_MESSAGE and source_of(e) == SRC_USER:
            return seq_of(e)
    return None


def plan_approved_seqs(events: list[dict[str, Any]]) -> list[int]:
    """Seqs of every `plan_approved` status, in order."""
    return [
        seq_of(e)
        for e in events
        if kind_of(e) == KIND_STATUS and (e.get("detail") or None) == PLAN_APPROVED_DETAIL
    ]


def awaiting_approval_seqs(events: list[dict[str, Any]]) -> list[int]:
    """Seqs of every AWAITING_PLAN_APPROVAL status, in order (the human-approval
    gate that must precede a RUNNING/plan_approved in an interactive run)."""
    return [
        seq_of(e)
        for e in events
        if kind_of(e) == KIND_STATUS
        and str(e.get("status")) == AWAITING_PLAN_APPROVAL_STATUS
    ]

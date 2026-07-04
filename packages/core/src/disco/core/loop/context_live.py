"""Flag-gated live context helpers for CXT runtime wiring."""

from __future__ import annotations

from ..env import disco_env
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    ObservationEvent,
    VerifierVerdictEvent,
)

_TRUTHY = {"1", "true", "yes", "on"}


def context_pack_enabled() -> bool:
    """True iff DISCO_CONTEXT_PACK is truthy. Default OFF."""
    return (disco_env("CONTEXT_PACK", "off") or "").strip().lower() in _TRUTHY


def unresolved_failure_seqs(events: list[Event]) -> frozenset[int]:
    """Seqs for failures that snip marks must not compact away.

    Tool/agent errors are conservative: without an explicit resolution event, keep
    them. Host verifier failures are keyed by artifact; a later passed verdict for
    the same artifact resolves earlier failed verdict events.
    """
    protected: set[int] = set()
    resolved_verifier_keys: set[tuple[str, str]] = set()
    for e in reversed(events):
        seq = e.seq
        if isinstance(e, VerifierVerdictEvent):
            key = (e.artifact_kind, e.artifact_path)
            if e.verified:
                resolved_verifier_keys.add(key)
            elif key not in resolved_verifier_keys and seq is not None:
                protected.add(seq)
        elif isinstance(e, AgentErrorEvent) and seq is not None:
            protected.add(seq)
        elif (
            isinstance(e, ObservationEvent)
            and not e.tool_result.success
            and seq is not None
        ):
            protected.add(seq)
    return frozenset(protected)


def latest_action_pair_seqs(events: list[Event]) -> frozenset[int]:
    """Seqs for the latest action and its paired observation/error, if present."""
    latest: ActionEvent | None = None
    for e in reversed(events):
        if isinstance(e, ActionEvent):
            latest = e
            break
    if latest is None:
        return frozenset()

    protected: set[int] = set()
    if latest.seq is not None:
        protected.add(latest.seq)
    call_id = latest.tool_call.call_id if latest.tool_call is not None else None
    for e in reversed(events):
        if isinstance(e, ObservationEvent):
            if e.action_id == latest.id or e.tool_result.call_id == call_id:
                if e.seq is not None:
                    protected.add(e.seq)
                break
        elif isinstance(e, AgentErrorEvent):
            if e.action_id == latest.id or e.tool_call_id == call_id:
                if e.seq is not None:
                    protected.add(e.seq)
                break
    return frozenset(protected)


def protected_context_compaction_seqs(events: list[Event]) -> frozenset[int]:
    return unresolved_failure_seqs(events) | latest_action_pair_seqs(events)

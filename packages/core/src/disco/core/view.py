"""Memory: View + Condensation — event-state-contract.md §5.

The tension: an append-only log grows without bound, but the LLM context window
is finite. Resolution (BoD §7.3): never delete; to "forget", append a
`CondensationEvent` tombstone recording a span to drop and a summary to insert.
The `View` computes, at read time, "what the LLM currently sees" by applying
those tombstones. The agent loop and LLM router consume `View.messages` — never
the raw log.

The View's behavior and tombstone semantics are [CONTRACT]. The *condensation
strategy* (when/how much to summarize) is a swappable [INTERIOR] `Condenser`.

Reversible-compaction tier (C11): the tombstone is a MARKER, not a delete —
the dropped events stay on the append-only log. `recover_span(events,
tombstone)` (and `View.recover_span`) re-materializes the originals on demand.

The projection, plan-progress, and condensation helpers live in the allowlisted
private modules (``_view_projection``, ``_view_plan``, ``_view_condensation``).
This module keeps ``View`` a ``BaseModel`` and preserves its declared surface.
"""

from __future__ import annotations

# Compatibility facades intentionally re-export their extracted symbols.
# ruff: noqa: F401
import hashlib
import logging

from pydantic import BaseModel

from . import _view_condensation as _view_condensation_module
from ._view_condensation import (
    CondensationRequest,
    Condenser,
    LLMSummarizingCondenser,
    NoOpCondenser,
    Summarizer,
    _build_pointer_manifest,
)
from ._view_plan import (
    _PROTOCOL_MARKERS,
    _PROTOCOL_TAG,
    _SUMMARY_REPAIR_INSTRUCTION,
    _fallback_summary,
    _latest_plan,
    effective_plan_progress,
    summary_rejection_reason,
)
from ._view_projection import (
    _DURABLE_TOOLS,
    _MASK_KEEP_RECENT,
    _MASK_MIN_CHARS,
    _SCREENSHOT_TOOLS,
    _call_key,
    _live_runtime_constraint_seqs,
    _pinned_seqs,
    _recitation_message,
    microcompact,
    recover_span,
    repair_tool_call_adjacency,
)
from ._view_projection import (
    build_view_messages as _build_view_messages,
)
from .env import disco_env
from .events import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    DatasourceEvent,
    Event,
    EventSource,
    KnowledgeEvent,
    LLMConvertible,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    RuntimeConstraintEvent,
    StatusEvent,
    agent_view_consistent_events,
)

# Preserve the historical import/pickle origin for the public condensation API.
for _compat_type in (
    CondensationRequest,
    Condenser,
    LLMSummarizingCondenser,
    NoOpCondenser,
    Summarizer,
):
    _compat_type.__module__ = __name__
del _compat_type

_LOG = logging.getLogger("disco.view")


class View(BaseModel):
    """The materialized "what the LLM sees right now", computed from the raw
    event log by applying condensation tombstones.

    [CONTRACT] View.of(events) is pure: same events -> same messages, every
    time. Computing it has no side effects and never appends.
    """

    messages: list[LLMMessage]
    visible_seqs: list[int]
    total_events: int
    forgotten_count: int

    @classmethod
    def of(cls, events: list[Event]) -> View:
        msgs, visible, total, forgotten_n = _build_view_messages(events)
        return cls(
            messages=msgs,
            visible_seqs=visible,
            total_events=total,
            forgotten_count=forgotten_n,
        )

    def fingerprint(self) -> list[str]:
        """[CONTRACT BP-06/B5] Stable per-message hashes for KV-prefix verification."""
        return [
            hashlib.sha256(
                f"{m.role}{m.content}{''.join(m.images) if m.images else ''}".encode()
            ).hexdigest()[:16]
            for m in self.messages
        ]

    @classmethod
    def recover_span(cls, events: list[Event], tombstone: CondensationEvent) -> list[Event]:
        """C11 — Reverse of condensation for a single tombstone. Returns the
        events the tombstone dropped from ``View.of(events).messages``."""
        return recover_span(events, tombstone)


# Preserve runtime type-hint resolution without importing this facade from the
# condenser module while it is still initializing.
_view_condensation_module.View = View  # pyright: ignore[reportAttributeAccessIssue]

"""DISCO_INSPECT — per-conversation request trace for end-to-end provability.

The testing rule is "prove the process works from the UI all the way to the other
end of the application". This module is the machinery for the *other end*: behind
``DISCO_INSPECT=1`` the runtime captures, per conversation, the full chain of

  (a) ROUTING decisions — which model handled each role, the path (pinned/manual/
      overflow), retry count, terminal failures — via a real :class:`RoutingSink`
      bound to the conversation, and
  (b) timed SPANS from :mod:`disco.core.obs` (``agent.step`` + its measured
      token/finish fields) via a logging handler on the ``disco.span`` logger,
      and
  (c) TOOL SCOPE snapshots immediately before each model request: the exact
      offered names plus the mode-specific names that remained callable.  Tool
      names and counts are capability metadata; arguments and schemas are never
      captured.

A REST snapshot endpoint (agent-server ``/api/debug/trace/{cid}``) exposes the
trace so a test — or a developer — can PROVE a request travelled UI → loop →
router → provider → back, without grepping prod logs.

Zero cost when off: :func:`inspect_enabled` is ``False``, the runtime installs no
handler and gives the router a ``NullRoutingSink``, so neither seam allocates
anything. Bounded when on: a ring of the most-recent ``_MAX_CONVERSATIONS`` traces,
each capped at ``_MAX_EVENTS`` events, so a long-running server (or a runaway loop)
can't grow the buffer without limit.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from .env import disco_env
from .llm.routing import RoutingSink
from .llm.types import RoutingDecision

# Ring bounds. Inspect is a developer/test affordance, not durable storage — keep
# the most-recent conversations and cap per-conversation depth so the buffer is
# O(_MAX_CONVERSATIONS * _MAX_EVENTS) regardless of uptime or loop length.
_MAX_CONVERSATIONS = 64
_MAX_EVENTS = 1024
_MAX_TOOL_SCOPE_NAMES = 512
_MAX_TOOL_NAME_CHARS = 256

_TRUTHY = {"1", "true", "yes", "on"}


def inspect_enabled() -> bool:
    """True iff ``DISCO_INSPECT`` (or legacy ``PMX_INSPECT``) is set truthy."""
    return (disco_env("INSPECT") or "").strip().lower() in _TRUTHY


# ---- trace data --------------------------------------------------------------


@dataclass(frozen=True)
class TraceEvent:
    """One entry in a conversation's trace. ``kind`` is ``"routing"`` (a
    RoutingDecision) or ``"span"`` (a disco.span record); ``data`` is the
    JSON-ready field dict. ``seq`` is a global monotonically-increasing counter so
    routing and span events interleave in true emission order in a snapshot."""

    seq: int
    kind: str
    data: dict[str, Any]


class ConversationTrace:
    """The bounded event log for a single conversation."""

    def __init__(self, conversation_id: str, max_events: int) -> None:
        self.conversation_id = conversation_id
        self.events: deque[TraceEvent] = deque(maxlen=max_events)
        self.dropped_event_count = 0

    def add(self, event: TraceEvent) -> None:
        if self.events.maxlen is not None and len(self.events) >= self.events.maxlen:
            self.dropped_event_count += 1
        self.events.append(event)

    def snapshot(self) -> dict[str, Any]:
        """A JSON-ready view: the interleaved event stream plus the routing /
        span projections (so a caller can assert on either dimension without
        re-filtering)."""
        evs = list(self.events)
        return {
            "conversation_id": self.conversation_id,
            "event_count": len(evs),
            "dropped_event_count": self.dropped_event_count,
            "routing_decisions": [e.data for e in evs if e.kind == "routing"],
            "spans": [e.data for e in evs if e.kind == "span"],
            "tool_scopes": [e.data for e in evs if e.kind == "tool_scope"],
            "events": [{"seq": e.seq, "kind": e.kind, **e.data} for e in evs],
        }


class InspectRegistry:
    """Thread-safe, bounded store of per-conversation traces.

    Thread-safety matters: span records arrive on whatever thread the logging
    framework flushes on, while routing decisions arrive on the asyncio loop
    thread. A single lock around the (cheap) append keeps the deque consistent.
    """

    def __init__(
        self,
        max_conversations: int = _MAX_CONVERSATIONS,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, ConversationTrace] = OrderedDict()
        self._seq = 0
        self._max_conversations = max_conversations
        self._max_events = max_events

    def _trace_for(self, cid: str) -> ConversationTrace:
        # Caller holds the lock. LRU: touch on access, evict oldest over the cap.
        tr = self._traces.get(cid)
        if tr is None:
            tr = ConversationTrace(cid, self._max_events)
            self._traces[cid] = tr
            while len(self._traces) > self._max_conversations:
                self._traces.popitem(last=False)
        self._traces.move_to_end(cid)
        return tr

    def add(self, cid: str | None, kind: str, data: dict[str, Any]) -> None:
        """Record one event. A falsy ``cid`` is dropped (an un-attributable span
        is noise, not a trace)."""
        if not cid:
            return
        with self._lock:
            self._seq += 1
            self._trace_for(cid).add(TraceEvent(self._seq, kind, data))

    def snapshot(self, cid: str) -> dict[str, Any] | None:
        with self._lock:
            tr = self._traces.get(cid)
            return tr.snapshot() if tr is not None else None

    def conversations(self) -> list[str]:
        """Most-recently-touched last (deque/LRU order)."""
        with self._lock:
            return list(self._traces.keys())

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._seq = 0


# Module-level singleton so the routing sink, the span handler, and the REST
# route all converge on one registry without threading it through constructors.
_REGISTRY: InspectRegistry | None = None


def registry() -> InspectRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = InspectRegistry()
    return _REGISTRY


# ---- routing seam ------------------------------------------------------------


def _decision_fields(d: RoutingDecision) -> dict[str, Any]:
    """Flatten a RoutingDecision to the trace's JSON-ready field dict."""
    return {
        "role": d.profile.role.value,
        "chosen_model": d.chosen_model,
        "provider": d.provider,
        "path": d.path,
        "reason": d.reason,
        "attempt": d.attempt,
        "overflow_triggers": list(d.overflow_triggers),
    }


class InspectRoutingSink:
    """A :class:`RoutingSink` that records each decision into the inspect registry
    under a FIXED conversation id. The router is built per-conversation (see
    ``ConversationRuntime._loop_for``), so binding the cid at construction is exact
    — every decision this router emits belongs to ``conversation_id``."""

    def __init__(self, conversation_id: str, reg: InspectRegistry | None = None) -> None:
        self._cid = conversation_id
        self._reg = reg or registry()

    def record(self, decision: RoutingDecision) -> None:
        self._reg.add(self._cid, "routing", _decision_fields(decision))


def routing_sink_for(conversation_id: str | None) -> RoutingSink | None:
    """The sink to hand a per-conversation router: a real inspect sink when
    inspect is enabled and a cid is known, else ``None`` (→ NullRoutingSink)."""
    if conversation_id and inspect_enabled():
        return InspectRoutingSink(conversation_id)
    return None


def record_tool_scope(
    conversation_id: str,
    *,
    mode: str,
    offered_tools: set[str],
    allowed_tools: set[str],
    attempt: int,
) -> None:
    """Record non-secret tool capability truth for one model request.

    This is deliberately inspect-only: when ``DISCO_INSPECT`` is off it neither
    creates the registry nor allocates sorted snapshots.  The caller supplies
    names only (never schemas or arguments), and the registry's existing event
    ring keeps the evidence bounded.
    """
    if not inspect_enabled():
        return
    offered_count = len(offered_tools)
    allowed_count = len(allowed_tools)
    complete = (
        offered_count <= _MAX_TOOL_SCOPE_NAMES
        and allowed_count <= _MAX_TOOL_SCOPE_NAMES
        and all(len(name) <= _MAX_TOOL_NAME_CHARS for name in offered_tools)
        and all(len(name) <= _MAX_TOOL_NAME_CHARS for name in allowed_tools)
    )
    # Do not sort an externally supplied, over-limit capability set.  The
    # persisted overflow marker remains constant-size and fail-closed.
    offered = sorted(offered_tools) if complete else []
    allowed = sorted(allowed_tools) if complete else []
    registry().add(
        conversation_id,
        "tool_scope",
        {
            "mode": mode,
            "attempt": attempt,
            "complete": complete,
            "offered_count": offered_count,
            "allowed_count": allowed_count,
            # Never silently preserve a prefix: without the complete set a
            # disallowed hidden tool could sit beyond the cap.  Empty lists plus
            # complete=false make selected classification INVALID.
            "offered_tools": offered,
            "allowed_tools": allowed,
        },
    )


# ---- span seam ---------------------------------------------------------------


class _SpanHandler(logging.Handler):
    """Captures ``disco.span`` records into the registry, attributed by the ``cid``
    field that :func:`disco.core.obs.log_span` stamps on each record."""

    def __init__(self, reg: InspectRegistry) -> None:
        super().__init__()
        self._reg = reg

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "_fields", None)
        if not isinstance(fields, dict):
            return
        cid = fields.get("cid")
        if cid:
            self._reg.add(str(cid), "span", dict(fields))


_INSTALLED = False


def install(reg: InspectRegistry | None = None) -> InspectRegistry:
    """Attach the span handler to the ``disco.span`` logger (idempotent). The
    runtime calls this at startup ONLY when ``inspect_enabled()`` — when off, no
    handler is ever attached and spans flow only to ordinary logging."""
    global _INSTALLED
    r = reg or registry()
    if not _INSTALLED:
        span_log = logging.getLogger("disco.span")
        # log_span emits at INFO; the logger inherits root's default WARNING unless
        # raised, which would drop every span BEFORE it reached our handler. Pin it
        # to INFO so spans actually flow to the inspect handler (independent of
        # whatever the root handler is configured to show).
        if span_log.level == logging.NOTSET or span_log.level > logging.INFO:
            span_log.setLevel(logging.INFO)
        span_log.addHandler(_SpanHandler(r))
        _INSTALLED = True
    return r

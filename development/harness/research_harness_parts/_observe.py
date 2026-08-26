"""Request model, redaction, phase classification, and the Observation recorder.

The JSONL event log is intentionally an outside-observer record: it contains
wire frames and cassette seams, not private product objects or credentials.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_DEPTH_TIMEOUTS_S = {
    # Wall-clock defaults derived from the product tier budgets (quick=360s,
    # standard=900s, exhaustive=2400s research time) plus report-writing time.
    "quick": 600.0,
    "standard_deep": 1500.0,
    "exhaustive": 3000.0,
}
_DECK_TIMEOUT_S = 1200.0
_SECRET_KEYS = re.compile(
    r"(?:secret|password|passwd|token|api[_-]?key|authorization|cookie|credential)", re.I
)
_SECRET_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"(?:sk|pk)[_-][A-Za-z0-9][A-Za-z0-9._-]{15,}|"
    r"LLM_[A-Za-z0-9][A-Za-z0-9._-]{20,}|"
    r"bearer\s+[A-Za-z0-9][A-Za-z0-9._-]{15,}"
    r")"
)
_CITATION = re.compile(r"\[\[([^\]]+)\]\]")
_FOOTNOTE_CITATION = re.compile(r"\[\^([^\]]+)\]")


@dataclass(frozen=True)
class ResearchRequest:
    query: str
    depth: str = "standard_deep"
    recency: str | None = None
    model: str | None = None
    provider: str | None = None
    surface: str = "deep_research"
    base_url: str = "http://localhost:8000"
    timeout_s: float | None = None
    cassette: str | None = None
    auth_token: str | None = None
    capture_inspect: bool = False
    # Report → deck is an explicitly opt-in follow-up.  It is deliberately a
    # request field instead of a transport-global switch so batch workers keep
    # their own bounded, isolated correlation state.
    deck: bool = False
    deck_timeout_s: float | None = None

    def __post_init__(self) -> None:
        # The default timeout is derived from the requested depth so a run is
        # never cut off short of its own tier budget; an explicit timeout_s
        # (CLI --timeout) still overrides it.
        if self.timeout_s is None:
            object.__setattr__(self, "timeout_s", _DEPTH_TIMEOUTS_S.get(self.depth, 1500.0))
        if self.deck_timeout_s is None:
            object.__setattr__(self, "deck_timeout_s", _DECK_TIMEOUT_S)

    def wire_body(self) -> dict[str, Any]:
        """Return the superset accepted by the current server routes.

        ``/ws/research`` currently consumes model_override and sources; the
        conversation API consumes the deep-research fields.  Sending the
        harmless superset lets this harness exercise either surface while its
        telemetry still records what was requested versus what was supported.
        """
        body: dict[str, Any] = {
            "query": self.query,
            "depth_tier": self.depth,
            "recency_window": self.recency,
            "model_override": self.model,
        }
        if self.provider:
            body["provider"] = self.provider
            body["sources"] = [self.provider]
        return {key: value for key, value in body.items() if value is not None}


@dataclass(frozen=True)
class ObservationEvent:
    seq: int
    elapsed_ms: int
    kind: str
    phase: str
    payload: dict[str, Any]


@dataclass
class Observation:
    started: float = field(default_factory=time.monotonic)
    events: list[ObservationEvent] = field(default_factory=list)
    # Model-call records are optional because the public WebSocket does not
    # expose provider prompts by default. When DISCO_INSPECT is enabled, the
    # live transport can attach the bounded inspect snapshot here.
    model_io: list[dict[str, Any]] = field(default_factory=list)
    # Provider-attempt lifecycle metadata is kept separate from model I/O so
    # retries never inflate prompt/response counts or expose model content.
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    inspect_trace: dict[str, Any] | None = None
    probes: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bounds: list[str] = field(default_factory=list)
    source_count: int = 0
    citation_count: int = 0

    def record(self, payload: Mapping[str, Any], *, kind: str = "frame") -> None:
        item = redact(dict(payload))
        phase = phase_for(item)
        event = ObservationEvent(
            seq=len(self.events) + 1,
            elapsed_ms=max(0, round((time.monotonic() - self.started) * 1000)),
            kind=kind,
            phase=phase,
            payload=item,
        )
        self.events.append(event)
        self._collect_model_io(item)
        self._collect(item, phase)

    def record_model_io(self, payload: Mapping[str, Any]) -> None:
        """Attach one already-redacted model-call record to this run.

        The harness deliberately does not infer hidden reasoning from provider
        internals.  Callers provide visible request/response content and
        declared decision metadata through the inspect seam.
        """
        self.model_io.append(redact(dict(payload)))

    def record_provider_attempt(self, payload: Mapping[str, Any]) -> None:
        """Attach bounded provider-attempt metadata from the inspect seam."""
        self.provider_attempts.append(redact(dict(payload)))

    def record_inspect_trace(self, payload: Mapping[str, Any]) -> None:
        """Attach the bounded per-conversation inspect snapshot."""
        snapshot = redact(dict(payload))
        self.inspect_trace = snapshot
        trace: Mapping[str, Any] = snapshot
        nested = trace.get("inspect_trace")
        if isinstance(nested, Mapping):
            trace = nested
        values = trace.get("model_attempts")
        has_attempt_projection = isinstance(values, list)
        if not has_attempt_projection:
            values = [
                event
                for event in trace.get("events", [])
                if isinstance(event, Mapping) and event.get("kind") == "model_attempt"
            ]
        if not has_attempt_projection and not values and "model_attempts" not in trace:
            return
        self.provider_attempts.clear()
        for item in values:
            if isinstance(item, Mapping):
                self.record_provider_attempt(item)

    def _collect(self, item: Mapping[str, Any], phase: str) -> None:
        self._collect_probes(item, phase)
        for key, value in _walk_fields(item):
            if key == "bounded_by" and value:
                self.bounds.append(str(value))
        self._collect_error(item)

    def _collect_model_io(self, item: Mapping[str, Any]) -> None:
        # Some transports/projectors surface model calls as a nested model_io
        # field.  Preserve one record per field and avoid recursively treating
        # inspect metadata as a second call.
        value = item.get("model_io")
        if isinstance(value, Mapping):
            self.record_model_io(value)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, Mapping):
                    self.record_model_io(row)

    def _collect_probes(self, item: Mapping[str, Any], phase: str) -> None:
        for key, value in _walk_fields(item):
            if key not in {"query", "issued_query", "issued_queries", "search_query", "probe"}:
                continue
            values = value if isinstance(value, list) else [value]
            for query in values:
                if isinstance(query, str) and query.strip():
                    row = {"query": query.strip(), "phase": phase}
                    if row not in self.probes:
                        self.probes.append(row)

    def _collect_error(self, item: Mapping[str, Any]) -> None:
        nested = item.get("event")
        event = nested if isinstance(nested, Mapping) else item
        frame_type = str(item.get("type") or "").casefold()
        event_kind = str(event.get("kind") or "").casefold()
        state = item.get("state")
        state_status = state.get("execution_status") if isinstance(state, Mapping) else None
        status_error = (
            event_kind == "status" and str(event.get("status") or "").upper() == "ERROR"
        ) or (
            frame_type == "state"
            and str(item.get("status") or state_status or "").upper() == "ERROR"
        )
        terminal_error = (
            frame_type == "error"
            or event_kind in {"error", "agent_error"}
            or status_error
        )
        if terminal_error:
            message = self._error_message(item)
            if message:
                self.errors.append(str(message))

    @staticmethod
    def _error_message(item: Mapping[str, Any]) -> Any:
        message = item.get("message") or item.get("error") or item.get("detail")
        if not message:
            message = next(
                (
                    value
                    for key, value in _walk_fields(item)
                    if key in {"message", "error", "detail"} and isinstance(value, str) and value
                ),
                None,
            )
        if isinstance(message, Mapping):
            message = message.get("message") or message.get("detail")
        return message

    def finalize(self, report: Mapping[str, Any]) -> None:
        passages = report.get("passages") or report.get("sources") or []
        citations = report.get("cited_passage_ids") or []
        self.source_count = len(passages) if isinstance(passages, list) else 0
        if isinstance(citations, list):
            self.citation_count = len(citations)
        else:
            self.citation_count = len(_citation_ids(report))

    @property
    def actual_models(self) -> list[str]:
        values: list[str] = []
        for event in self.events:
            for key, value in _walk_fields(event.payload):
                if key in {"model", "model_used", "chosen_model", "actual_model"} and value:
                    text = str(value)
                    if text not in values:
                        values.append(text)
        for item in [*self.model_io, *self.provider_attempts]:
            for key, value in _walk_fields(item):
                if key in {"model", "model_used", "chosen_model", "actual_model"} and value:
                    text = str(value)
                    if text not in values:
                        values.append(text)
        return values

    @property
    def actual_providers(self) -> list[str]:
        values: list[str] = []
        sources = [
            *(event.payload for event in self.events),
            *self.model_io,
            *self.provider_attempts,
        ]
        for source in sources:
            for key, value in _walk_fields(source):
                if key in {"provider", "provider_used", "actual_provider"} and value:
                    text = str(value)
                    if text not in values:
                        values.append(text)
        return values


def redact(value: Any, *, _key: str = "") -> Any:
    if _key and _SECRET_KEYS.search(_key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(key): redact(item, _key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


def phase_for(payload: Mapping[str, Any]) -> str:
    direct = " ".join(
        str(payload.get(key) or "") for key in ("type", "kind", "phase", "name", "span")
    ).lower()
    labelled = _labelled_phase(direct)
    if labelled:
        return labelled
    text = json.dumps(payload, ensure_ascii=False, default=str).lower()
    from_text = _payload_text_phase(text)
    if from_text:
        return from_text
    if str(payload.get("type") or "").lower() == "final":
        return "final"
    return "lifecycle"


def _labelled_phase(direct: str) -> str | None:
    """Classify from the frame's own type/kind/phase labels (checked first)."""
    if "final" in direct:
        return "final"
    if any(word in direct for word in ("synth", "summary", "report")):
        return "synthesis"
    if any(word in direct for word in ("verif", "ground", "nli", "claim")):
        return "verification"
    if any(word in direct for word in ("gap", "missing", "coverage")):
        return "gap"
    if any(word in direct for word in ("extract", "fetch")):
        return "extract"
    if any(word in direct for word in ("search", "query", "probe")):
        return "search"
    return None


def _payload_text_phase(text: str) -> str | None:
    """Fall back to the serialized payload when the labels were inconclusive."""
    if any(word in text for word in ("search", "query", "probe")):
        return "search"
    if any(word in text for word in ("extract", "fetch", "source")):
        return "extract"
    if any(word in text for word in ("gap", "missing", "coverage")):
        return "gap"
    if any(word in text for word in ("synth", "summary", "report")):
        return "synthesis"
    if any(word in text for word in ("verif", "ground", "nli", "claim")):
        return "verification"
    if any(word in text for word in ("plan", "decompos")):
        return "planning"
    return None


def _citation_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"cited_passage_ids", "citations"} and isinstance(item, list):
                found.update(str(entry) for entry in item if isinstance(entry, (str, int)))
            elif key in {"markdown", "answer_markdown", "text", "summary"} and isinstance(
                item, str
            ):
                found.update(_CITATION.findall(item))
                found.update(_FOOTNOTE_CITATION.findall(item))
            elif key not in {"passages", "reviewed_passages", "all_hits", "sources", "hits"}:
                found.update(_citation_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_citation_ids(item))
    return found


def _walk_fields(value: Any) -> list[tuple[str, Any]]:
    """Yield nested key/value pairs from a redacted wire frame."""
    if isinstance(value, Mapping):
        fields: list[tuple[str, Any]] = []
        for key, item in value.items():
            fields.append((str(key), item))
            fields.extend(_walk_fields(item))
        return fields
    if isinstance(value, list):
        fields = []
        for item in value:
            fields.extend(_walk_fields(item))
        return fields
    return []


def _phase_counts(events: Sequence[ObservationEvent]) -> dict[str, int]:
    counts = {phase: 0 for phase in ("search", "extract", "gap", "synthesis", "verification")}
    for event in events:
        counts[event.phase] = counts.get(event.phase, 0) + 1
    return counts

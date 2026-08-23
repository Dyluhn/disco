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
_SECRET_KEYS = re.compile(
    r"(?:secret|password|passwd|token|api[_-]?key|authorization|cookie|credential)", re.I
)
_SECRET_VALUE = re.compile(r"(?i)\b(?:sk|pk|key|token|bearer)[_-]?[A-Za-z0-9][A-Za-z0-9._-]{15,}")
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

    def __post_init__(self) -> None:
        # The default timeout is derived from the requested depth so a run is
        # never cut off short of its own tier budget; an explicit timeout_s
        # (CLI --timeout) still overrides it.
        if self.timeout_s is None:
            object.__setattr__(self, "timeout_s", _DEPTH_TIMEOUTS_S.get(self.depth, 1500.0))

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
        self._collect(item, phase)

    def _collect(self, item: Mapping[str, Any], phase: str) -> None:
        self._collect_probes(item, phase)
        for key, value in _walk_fields(item):
            if key == "bounded_by" and value:
                self.bounds.append(str(value))
        self._collect_error(item)

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
        text = json.dumps(item, ensure_ascii=False).lower()
        status_error = any(
            key == "status" and str(value).upper() == "ERROR" for key, value in _walk_fields(item)
        )
        if item.get("type") == "error" or item.get("error") or "exception" in text or status_error:
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
                    if key in {"message", "error", "detail"}
                    and isinstance(value, str)
                    and value
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

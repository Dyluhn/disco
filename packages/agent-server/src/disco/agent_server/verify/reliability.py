"""Reliability metrics derived from verify event logs.

The verify runner and dashboard consume JSON event logs, so these helpers accept
dict-like events and intentionally avoid depending on live loop state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from disco.core.contract.export_render import EXPORT_GATE_TOKEN
from disco.core.loop.signals import SYNTHETIC_FINISH_ATTEMPT_DETAIL

ACTIONLESS_PAUSE_DETAIL = "actionless"
ACTIONLESS_AUTO_RESUME_MARKER = "AUTO-RESUME-ONCE(actionless)"
STUCK_ESCAPE_DETAIL = "stuck_escape"
PROBE_SPIN_DETAIL = "probe_spin"
EXPORT_RELEASE_DETAIL = "unverified_export"
HOST_VERIFY_REFUSAL_MARKER = "Host verification did not pass"
BLOCKED_LANDING_META_KEY = "blocked_landing"

_ENVIRONMENT_SOURCE = "environment"
_MESSAGE_KIND = "message"
_STATUS_KIND = "status"
_PAUSED_STATUS = "PAUSED"
_NON_STALLED_STATUSES = frozenset({"FINISHED", "ERROR", "IDLE"})
_COUNTER_KEYS = (
    "actionless_pauses",
    "auto_resumes",
    "synthetic_finishes",
    "stuck_escapes",
    "probe_spin_trips",
    "export_refusals",
    "export_releases",
    "host_verify_refusals",
    "blocked_landings",
)


class ReliabilityMetrics(TypedDict):
    actionless_pauses: int
    auto_resumes: int
    synthetic_finishes: int
    stuck_escapes: int
    probe_spin_trips: int
    export_refusals: int
    export_releases: int
    host_verify_refusals: int
    blocked_landings: int
    terminal_status: str
    stalled: bool


def _field(obj: object, name: str, default: object = None) -> object:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: object) -> str:
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if value is None:
        return ""
    return str(value)


def _kind(event: object) -> str:
    return _text(_field(event, "kind")).lower()


def _source(event: object) -> str:
    return _text(_field(event, "source")).lower()


def _detail(event: object) -> str:
    return _text(_field(event, "detail"))


def _status(event: object) -> str:
    return _text(_field(event, "status"))


def _message_content(event: object) -> str:
    message = _field(event, "message")
    content = _field(message, "content")
    if isinstance(content, str):
        return content
    fallback = _field(event, "content")
    return fallback if isinstance(fallback, str) else ""


def _meta(event: object) -> Mapping[str, Any]:
    meta = _field(event, "meta")
    return meta if isinstance(meta, Mapping) else {}


def _is_environment_message(event: object) -> bool:
    return _kind(event) == _MESSAGE_KIND and _source(event) == _ENVIRONMENT_SOURCE


def _is_status(event: object) -> bool:
    return _kind(event) == _STATUS_KIND


def run_reliability_metrics(events: Iterable[object]) -> ReliabilityMetrics:
    """Return reliability counters for one verify run's event log."""

    actionless_pauses = 0
    auto_resumes = 0
    synthetic_finishes = 0
    stuck_escapes = 0
    probe_spin_trips = 0
    export_refusals = 0
    export_releases = 0
    host_verify_refusals = 0
    blocked_landings = 0
    terminal_status = "UNKNOWN"

    for event in events:
        if _is_status(event):
            status = _status(event)
            detail = _detail(event)
            terminal_status = status or terminal_status
            if status == _PAUSED_STATUS and detail == ACTIONLESS_PAUSE_DETAIL:
                actionless_pauses += 1
            if detail == SYNTHETIC_FINISH_ATTEMPT_DETAIL:
                synthetic_finishes += 1
            if detail == STUCK_ESCAPE_DETAIL:
                stuck_escapes += 1
            if detail == PROBE_SPIN_DETAIL:
                probe_spin_trips += 1
            if detail == EXPORT_RELEASE_DETAIL:
                export_releases += 1
            if _meta(event).get(BLOCKED_LANDING_META_KEY) is True:
                blocked_landings += 1
            continue

        if not _is_environment_message(event):
            continue
        content = _message_content(event)
        if ACTIONLESS_AUTO_RESUME_MARKER in content:
            auto_resumes += 1
        if EXPORT_GATE_TOKEN in content:
            export_refusals += 1
        if HOST_VERIFY_REFUSAL_MARKER in content:
            host_verify_refusals += 1

    return {
        "actionless_pauses": actionless_pauses,
        "auto_resumes": auto_resumes,
        "synthetic_finishes": synthetic_finishes,
        "stuck_escapes": stuck_escapes,
        "probe_spin_trips": probe_spin_trips,
        "export_refusals": export_refusals,
        "export_releases": export_releases,
        "host_verify_refusals": host_verify_refusals,
        "blocked_landings": blocked_landings,
        "terminal_status": terminal_status,
        "stalled": terminal_status not in _NON_STALLED_STATUSES,
    }


def _metrics_from_run(run: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = run.get("reliability_metrics")
    if isinstance(metrics, Mapping):
        return metrics
    result = run.get("result")
    if isinstance(result, Mapping):
        nested = result.get("reliability_metrics")
        if isinstance(nested, Mapping):
            return nested
    return run


def _metric_int(metrics: Mapping[str, Any], key: str) -> int:
    value = metrics.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


def aggregate_reliability_metrics(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate reliability metrics for a list of run reports or metric dicts."""

    totals = {key: 0 for key in _COUNTER_KEYS}
    run_count = 0
    stalled_count = 0

    for run in runs:
        run_count += 1
        metrics = _metrics_from_run(run)
        for key in _COUNTER_KEYS:
            totals[key] += _metric_int(metrics, key)
        if metrics.get("stalled") is True:
            stalled_count += 1

    totals["stalled"] = stalled_count
    totals["runs"] = run_count
    return {
        "stall_rate": stalled_count / run_count if run_count else 0.0,
        "totals": totals,
    }


__all__ = [
    "ACTIONLESS_AUTO_RESUME_MARKER",
    "ACTIONLESS_PAUSE_DETAIL",
    "EXPORT_GATE_TOKEN",
    "EXPORT_RELEASE_DETAIL",
    "HOST_VERIFY_REFUSAL_MARKER",
    "PROBE_SPIN_DETAIL",
    "ReliabilityMetrics",
    "STUCK_ESCAPE_DETAIL",
    "SYNTHETIC_FINISH_ATTEMPT_DETAIL",
    "aggregate_reliability_metrics",
    "run_reliability_metrics",
]

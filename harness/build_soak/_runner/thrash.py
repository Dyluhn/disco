"""Bounded Build Soak thrash owner."""

from __future__ import annotations

import math
from typing import Any

from .. import failure_codes as fc
from ..adapters.disco_api import (
    PAUSED_STATE,
    TERMINAL_STATES,
    CollectedRun,
    DiscoApiClient,
)
from ..events import NormalizationError, normalize_events
from ..oracles.thrash import ThrashOracle
from .temporal import (
    _event_epoch,
    _min_event_epoch,
    _status_value_and_detail,
)


def _first_killed_idle_epoch(events: list[dict[str, Any]]) -> float | None:
    first: float | None = None
    for event in events:
        if event.get("kind") != "status":
            continue
        status, detail = _status_value_and_detail(event)
        epoch = _event_epoch(event)
        if status == "IDLE" and detail == "killed" and epoch is not None:
            first = epoch if first is None else min(first, epoch)
    return first


def _monitor_counts(monitor: dict[str, Any]) -> tuple[int, int] | None:
    minimum = monitor.get("minimum_confirmation_samples")
    sample_count = monitor.get("sample_count")
    if type(minimum) is not int or minimum < 2:
        return None
    if type(sample_count) is not int or sample_count < minimum:
        return None
    return minimum, sample_count


def _finding_counts_valid(finding: dict[str, Any], *, minimum: int, sample_count: int) -> bool:
    confirmations = finding.get("confirmation_samples")
    event_count = finding.get("event_count")
    max_event_seq = finding.get("max_event_seq")
    return (
        type(confirmations) is int
        and minimum <= confirmations <= sample_count
        and type(event_count) is int
        and event_count >= 0
        and type(max_event_seq) is int
        and max_event_seq >= -1
    )


def _finding_timestamp_valid(detected_at: object, *, run_start: float, killed_at: float) -> bool:
    return (
        isinstance(detected_at, (int, float))
        and not isinstance(detected_at, bool)
        and math.isfinite(detected_at)
        and run_start <= float(detected_at) <= killed_at
    )


def _finding_status_valid(terminal_status: object) -> bool:
    return (
        isinstance(terminal_status, str)
        and bool(terminal_status)
        and terminal_status not in TERMINAL_STATES
        and terminal_status != PAUSED_STATE
    )


def _confirmed_finding(
    finding: object,
    *,
    minimum: int,
    sample_count: int,
    run_start: float,
    killed_at: float,
    thrash_codes: set[str],
) -> bool:
    if not isinstance(finding, dict):
        return False
    detected_at = finding.get("detected_at_epoch")
    terminal_status = finding.get("terminal_status")
    results = finding.get("oracle_results")
    if (
        not _finding_counts_valid(finding, minimum=minimum, sample_count=sample_count)
        or not _finding_timestamp_valid(detected_at, run_start=run_start, killed_at=killed_at)
        or not _finding_status_valid(terminal_status)
        or not isinstance(results, list)
    ):
        return False
    return any(
        isinstance(result, dict)
        and result.get("status") == fc.FAIL
        and result.get("oracle") == "ThrashOracle"
        and result.get("code") in thrash_codes
        for result in results
    )


def _strict_live_thrash_stop_boundary(
    events: list[dict[str, Any]],
    state_final: dict[str, Any],
    monitor: dict[str, Any],
) -> bool:
    """Whether strict retained monitor evidence proves a pre-kill thrash stop.

    The monitor stops an actively thrashing conversation through ``POST /kill``.
    The resulting durable terminal is ``IDLE(detail=killed)``, not a normal Build
    work terminal.  Accept that otherwise-ambiguous marker only when the retained
    monitor record contains a confirmed strict ThrashOracle failure.
    """

    if DiscoApiClient._status_of(state_final) != "IDLE":
        return False
    if not isinstance(monitor, dict) or monitor.get("enabled") is not True:
        return False
    counts = _monitor_counts(monitor)
    if counts is None:
        return False
    minimum, sample_count = counts
    thrash_codes = {
        fc.TOOL_CALL_THRASH,
        fc.TOOL_ERROR_THRASH,
        fc.ACTIONLESS_THRASH,
        fc.MODEL_REPAIR_THRASH,
    }
    findings = monitor.get("findings")
    if not isinstance(findings, list):
        return False
    run_start = _min_event_epoch(events)
    killed_at = _first_killed_idle_epoch(events)
    if run_start is None or killed_at is None or run_start > killed_at:
        return False
    return any(
        _confirmed_finding(
            finding,
            minimum=minimum,
            sample_count=sample_count,
            run_start=run_start,
            killed_at=killed_at,
            thrash_codes=thrash_codes,
        )
        for finding in findings
    )


def _confirmed_live_thrash_stop(run: CollectedRun) -> bool:
    """Whether the frozen run satisfies the strict pre-kill monitor boundary."""

    return _strict_live_thrash_stop_boundary(
        run.events,
        run.state_final,
        run.thrash_monitor,
    )


def _current_thrash_failures(
    run: CollectedRun, scenario: dict[str, Any]
) -> list[dict[str, Any]] | None:
    try:
        return [
            result.to_dict()
            for result in ThrashOracle().check(
                normalize_events(run.events),
                scenario=scenario,
                inspect_trace=run.inspect_trace,
            )
            if result.failed
        ]
    except (NormalizationError, ValueError, TypeError):
        return None


def _retained_thrash_failures(run: CollectedRun) -> list[dict[str, Any]]:
    return [
        result
        for finding in (run.thrash_monitor or {}).get("findings") or []
        if isinstance(finding, dict)
        for result in finding.get("oracle_results") or []
        if isinstance(result, dict)
        and result.get("oracle") == "ThrashOracle"
        and result.get("status") == fc.FAIL
    ]


def _same_thrash_failure(observed: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    if not isinstance(observed.get("first_broken_link"), str) or not isinstance(
        observed.get("facts"), dict
    ):
        return False
    return any(
        candidate.get("code") == observed.get("code")
        and candidate.get("first_broken_link") == observed.get("first_broken_link")
        and candidate.get("facts") == observed.get("facts")
        for candidate in candidates
    )


def _current_thrash_failure_matches_retained(run: CollectedRun, scenario: dict[str, Any]) -> bool:
    """Require current deterministic agreement with one retained monitor finding."""

    current = _current_thrash_failures(run, scenario)
    if current is None:
        return False
    retained = _retained_thrash_failures(run)
    for observed in retained:
        if _same_thrash_failure(observed, current):
            return True
    return False

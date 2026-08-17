"""Bounded Build Soak preflight owner."""

from __future__ import annotations

from typing import Any

from ..adapters.disco_api import (
    CollectedRun,
    DiscoApiClient,
)
from ..events import NormalizationError, normalize_events


def _terminal_driver_route(decision: object) -> bool:
    return (
        isinstance(decision, dict)
        and str(decision.get("role") or "") == "agent_driver"
        and bool(str(decision.get("chosen_model") or ""))
        and bool(str(decision.get("provider") or ""))
        and str(decision.get("reason") or "").startswith("terminal failure:")
    )


def _is_terminal_driver_preflight_trace(
    run: CollectedRun,
    trace: dict[str, Any],
    routing: object,
    agent_spans: list[dict[str, Any]],
) -> bool:
    """Whether a trace proves the driver failed before the agent loop could start.

    Driver readiness calls are deliberately outside ``agent.step``.  Requiring a
    loop span after a named terminal routing failure launders a genuine product /
    provider failure into missing-evidence INVALID_RUN.  The exception is narrow:
    terminal ERROR, non-empty all-terminal routing decisions, no tool-scope event,
    and no agent span.  Once the loop starts it records a tool scope before its
    model request, so a missing span after that boundary still fails closed.
    """

    tool_scopes = trace.get("tool_scopes")
    return (
        DiscoApiClient._status_of(run.state_final) == "ERROR"
        and trace.get("dropped_event_count") == 0
        and not agent_spans
        and isinstance(routing, list)
        and bool(routing)
        and all(_terminal_driver_route(decision) for decision in routing)
        and isinstance(tool_scopes, list)
        and not tool_scopes
    )


def _latest_durable_status(run: CollectedRun) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return the latest normalized status plus the corroborating durable log."""

    try:
        events = normalize_events(run.events)
    except NormalizationError:
        return None, []
    statuses = [event for event in events if event.get("kind") == "status"]
    return (statuses[-1] if statuses else None), events


def _named_sandbox_preflight_detail(detail: str) -> bool:
    """Match only reason strings produced by ``runtime._preflight_sandbox``."""

    if detail.startswith("sandbox backend is misconfigured:"):
        return bool(detail.removeprefix("sandbox backend is misconfigured:").strip())
    for prefix in (
        "gvisor sandbox host ",
        "local sandbox host ",
        "podman sandbox host ",
    ):
        if not detail.startswith(prefix):
            continue
        rest = detail.removeprefix(prefix)
        for marker in (" unreachable:", " error:"):
            endpoint, found, cause = rest.partition(marker)
            if found and endpoint.strip() and cause.strip():
                return True
        return False
    return any(
        detail.startswith(prefix) and bool(detail.removeprefix(prefix).strip())
        for prefix in ("process sandbox unreachable:", "process sandbox error:")
    )


def _system_sandbox_failure(latest_status: dict[str, Any]) -> bool:
    return (
        str(latest_status.get("source") or "") == "system"
        and str(latest_status.get("status") or "").upper() == "ERROR"
        and _named_sandbox_preflight_detail(str(latest_status.get("detail") or ""))
    )


def _sandbox_route_reason_matches(route: dict[str, Any]) -> bool:
    reason = str(route.get("reason") or "")
    overflow = route.get("overflow_triggers")
    return (reason == "config" and overflow == []) or (
        reason in {"shared driver preflight success", "cached driver preflight success"}
        and overflow == ["driver_preflight"]
    )


def _successful_sandbox_route(route: dict[str, Any], expected_model: str) -> bool:
    return (
        str(route.get("role") or "") == "agent_driver"
        and str(route.get("path") or "") in {"manual", "pinned"}
        and bool(str(route.get("chosen_model") or ""))
        and bool(str(route.get("provider") or ""))
        and type(route.get("attempt")) is int
        and route["attempt"] >= 1
        and _sandbox_route_reason_matches(route)
        and (not expected_model or route.get("chosen_model") == expected_model)
    )


def _trace_matches_route(trace: dict[str, Any], route: dict[str, Any]) -> bool:
    trace_events = trace.get("events")
    only_event = (
        trace_events[0]
        if isinstance(trace_events, list)
        and len(trace_events) == 1
        and isinstance(trace_events[0], dict)
        else None
    )
    fields = (
        "role",
        "chosen_model",
        "provider",
        "path",
        "reason",
        "attempt",
        "overflow_triggers",
    )
    return (
        trace.get("dropped_event_count") == 0
        and isinstance(trace_events, list)
        and trace.get("event_count") == len(trace_events)
        and only_event is not None
        and only_event.get("kind") == "routing"
        and all(only_event.get(field) == route.get(field) for field in fields)
        and all(event.get("kind") not in {"tool_scope", "span"} for event in trace_events)
    )


def _has_loop_events(events: list[dict[str, Any]]) -> bool:
    loop_kinds = {"plan", "action", "observation", "agent_error", "report"}
    return any(event.get("kind") in loop_kinds for event in events)


def _only_route(routing: object) -> dict[str, Any] | None:
    if not isinstance(routing, list) or len(routing) != 1:
        return None
    route = routing[0]
    return route if isinstance(route, dict) else None


def _is_terminal_sandbox_preflight_trace(
    run: CollectedRun,
    trace: dict[str, Any],
    routing: object,
    agent_spans: list[dict[str, Any]],
    scenario: dict[str, Any],
) -> bool:
    """Whether exact evidence proves sandbox readiness failed before the loop.

    Driver readiness succeeds first and records the exact chosen model/provider;
    sandbox readiness then runs before loop composition and therefore has no
    ``agent.step`` or tool scope.  Only the runtime's named, fail-closed sandbox
    error shapes are admitted.  A generic loop ERROR remains missing evidence.
    """

    latest_status, durable_events = _latest_durable_status(run)
    if latest_status is None:
        return False
    tool_scopes = trace.get("tool_scopes")
    spans = trace.get("spans")
    expected_model = str(
        (((scenario.get("assertions") or {}).get("provider") or {}).get("model")) or ""
    )
    route = _only_route(routing)
    if route is None:
        return False
    return (
        DiscoApiClient._status_of(run.state_final) == "ERROR"
        and _system_sandbox_failure(latest_status)
        and not agent_spans
        and spans == []
        and _successful_sandbox_route(route, expected_model)
        and _trace_matches_route(trace, route)
        and isinstance(tool_scopes, list)
        and not tool_scopes
        and not _has_loop_events(durable_events)
    )

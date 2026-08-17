"""Frozen inspect-trace tool-scope extraction (fail-closed)."""

from __future__ import annotations

from typing import Any

_MALFORMED = "__malformed__"


def _validate_trace_shape(
    inspect_trace: dict[str, Any],
) -> tuple[list[Any] | None, list[Any] | None, str | None]:
    """Validate the top-level trace shape. Returns (scopes, events, error)."""
    scopes = inspect_trace.get("tool_scopes")
    events = inspect_trace.get("events")
    if not isinstance(scopes, list) or not isinstance(events, list):
        return None, None, "tool_scopes/events must be lists"
    event_count = inspect_trace.get("event_count")
    if not isinstance(event_count, int) or event_count != len(events):
        return None, None, "event_count does not match events"
    dropped_event_count = inspect_trace.get("dropped_event_count")
    if not isinstance(dropped_event_count, int) or dropped_event_count != 0:
        return None, None, "inspect trace is truncated"
    return scopes, events, None


def _project_tool_scope_events(events: list[Any]) -> list[dict[str, Any]] | str | None:
    """Project tool_scope events, or return an error string."""
    projected: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            return "inspect event is not an object"
        if event.get("kind") != "tool_scope":
            continue
        item = {k: v for k, v in event.items() if k not in {"seq", "kind"}}
        projected.append(item)
    return projected


def _materialize_scopes(scopes: list[Any]) -> list[dict[str, Any]]:
    """Copy scope dicts, marking non-dict entries as malformed."""
    return [
        dict(scope) if isinstance(scope, dict) else {_MALFORMED: "scope is not an object"}
        for scope in scopes
    ]


def tool_scope_from_inspect(
    inspect_trace: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """Extract tool-scope proof from the frozen inspect trace, fail-closed.

    ``ConversationTrace.snapshot`` exposes both a convenience projection and an
    interleaved event stream.  Requiring them to agree prevents a partial or
    hand-edited projection from becoming admissible evidence.  A malformed
    capture is returned as a sentinel entry for ``ToolScopeOracle`` to classify
    as INVALID; total absence remains ``None`` so ``ContractOracle`` reports the
    selected assertion's missing evidence.
    """
    if inspect_trace is None or "tool_scopes" not in inspect_trace:
        return None
    scopes, events, error = _validate_trace_shape(inspect_trace)
    if error is not None:
        return [{_MALFORMED: error}]
    assert scopes is not None and events is not None
    projected = _project_tool_scope_events(events)
    if isinstance(projected, str):
        return [{_MALFORMED: projected}]
    if scopes != projected:
        return [{_MALFORMED: "tool_scopes projection disagrees with events"}]
    return _materialize_scopes(scopes)

"""Bounded Build Soak temporal owner."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _status_value_and_detail(event: dict[str, Any]) -> tuple[str, Any]:
    status = event.get("status")
    detail = event.get("detail")
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) or {}
        except Exception:
            payload = {}
    if isinstance(payload, dict):
        if status is None:
            status = payload.get("status")
        if detail is None:
            detail = payload.get("detail")
    return str(status or "").upper(), detail


def _event_epoch(e: dict[str, Any]) -> float | None:
    """Epoch of one event from its ISO-8601 UTC timestamp ('2026-06-30T16:59:28.755769Z')."""

    ts = e.get("timestamp") or e.get("created_at")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


_BUILD_TERMINAL_STATUSES = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})


def _terminal_status_epoch(
    events: list[dict[str, Any]], *, allow_killed_idle: bool = False
) -> float | None:
    """[codex/Lane A] Epoch of the LAST `status` event whose status is a BUILD terminal
    (_BUILD_TERMINAL_STATUSES — NOT IDLE) — the true instant the build went terminal. NOT the max
    timestamp across ALL events (a durable event appended AFTER the terminal status would push the
    anchor past a real post-terminal provider call), and NOT anchored on IDLE (the rest/kill state).
    None if no build-terminal status event is present."""
    best: float | None = None
    first_confirmed_kill: float | None = None
    for e in events:
        if e.get("kind") != "status":
            continue
        # run.events are raw DB rows {seq,kind,created_at,payload(JSON string)} — the status lives
        # INSIDE `payload`, not as a top-level field. Accept both shapes (flattened + DB-row).
        normalized_status, _detail = _status_value_and_detail(e)
        ep = _event_epoch(e)
        if ep is None:
            continue
        if normalized_status in _BUILD_TERMINAL_STATUSES and (best is None or ep > best):
            best = ep
        elif (
            allow_killed_idle
            and normalized_status == "IDLE"
            and _detail == "killed"
            and (first_confirmed_kill is None or ep < first_confirmed_kill)
        ):
            # The first kill is the monitor's spend stop.  Later idempotent cleanup
            # kills must not move the provider-after-terminal boundary forward.
            first_confirmed_kill = ep
    # When the caller has independently proven a monitor-owned kill, that kill is
    # the spend-stop boundary even if the conversation history contains an older
    # work terminal from a prior follow-up phase.  Ordinary terminal runs call with
    # allow_killed_idle=False and retain the latest work-terminal behavior.
    return first_confirmed_kill if first_confirmed_kill is not None else best


def _min_event_epoch(events: list[dict[str, Any]]) -> float | None:
    """Epoch of the build's FIRST event (run start) — the lower bound of this run's relay window."""
    best: float | None = None
    for e in events:
        ep = _event_epoch(e)
        if ep is not None and (best is None or ep < best):
            best = ep
    return best

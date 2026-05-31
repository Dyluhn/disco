"""Stuck detection — agent-loop-contract.md §6 (re-implemented from the SDK).

Checked every iteration before stepping. Pure function of the recent event
window; uses `event_content_eq` (event contract §6.3) so byte-differences in
ids/timestamps never mask a semantic loop.

Four patterns are implemented (BoD §12.5 patterns 1–4). Pattern 5 (the
context-window-error loop) is known-hard and is NOT detected here; its cause is
prevented by the hard-reset condensation (§8) and bounded by `max_iterations`.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..equality import event_content_eq
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
)


class StuckThresholds(BaseModel):
    repeat_action_observation: int = 3  # identical action→obs cycles
    repeat_action_error: int = 3  # identical action→error cycles
    agent_monologue: int = 4  # consecutive agent msgs, no user
    alternating: int = 3  # A-B-A-B cycles
    scan_window: int = 20  # only inspect the last N events


def _after_last_user_message(events: list[Event]) -> list[Event]:
    """Discard everything at/before the last USER MessageEvent — a new
    instruction means 'not stuck'."""
    last_user = -1
    for i, e in enumerate(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            last_user = i
    return events[last_user + 1 :]


def _consecutive_pairs(
    events: list[Event], first_type: type, second_type: type
) -> list[tuple[Event, Event]]:
    """Collect immediately-adjacent (first_type, second_type) event pairs."""
    pairs: list[tuple[Event, Event]] = []
    i = 0
    while i < len(events) - 1:
        a, b = events[i], events[i + 1]
        if isinstance(a, first_type) and isinstance(b, second_type):
            pairs.append((a, b))
            i += 2
        else:
            i += 1
    return pairs


class StuckDetector:
    """[CONTRACT] Pure stuck-pattern detection over the recent event window."""

    def __init__(self, thresholds: StuckThresholds | None = None) -> None:
        self.t = thresholds or StuckThresholds()

    def is_stuck(self, recent: list[Event]) -> bool:
        recent = _after_last_user_message(recent)
        return (
            self._repeated_action_observation(recent)
            or self._repeated_action_error(recent)
            or self._agent_monologue(recent)
            or self._alternating(recent)
        )

    # -- pattern 1: identical action→observation cycles -----------------------

    def _repeated_action_observation(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_observation
        pairs = _consecutive_pairs(events, ActionEvent, ObservationEvent)
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        a0, o0 = last[0]
        return all(event_content_eq(a, a0) and event_content_eq(o, o0) for a, o in last)

    # -- pattern 2: identical action→error cycles -----------------------------

    def _repeated_action_error(self, events: list[Event]) -> bool:
        n = self.t.repeat_action_error
        pairs = _consecutive_pairs(events, ActionEvent, AgentErrorEvent)
        if len(pairs) < n:
            return False
        last = pairs[-n:]
        a0, e0 = last[0]
        return all(event_content_eq(a, a0) and event_content_eq(e, e0) for a, e in last)

    # -- pattern 3: agent monologue (consecutive agent messages) --------------

    def _agent_monologue(self, events: list[Event]) -> bool:
        run = 0
        best = 0
        for e in events:
            if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
                run += 1
                best = max(best, run)
            else:
                run = 0
        return best >= self.t.agent_monologue

    # -- pattern 4: alternating A-B-A-B action loops --------------------------

    def _alternating(self, events: list[Event]) -> bool:
        cycles = self.t.alternating
        need = 2 * cycles
        actions = [e for e in events if isinstance(e, ActionEvent)]
        if len(actions) < need:
            return False
        window = actions[-need:]
        evens, odds = window[0::2], window[1::2]
        a, b = evens[0], odds[0]
        if event_content_eq(a, b):
            return False  # identical => that's pattern 1, not alternation
        return all(event_content_eq(x, a) for x in evens) and all(
            event_content_eq(y, b) for y in odds
        )

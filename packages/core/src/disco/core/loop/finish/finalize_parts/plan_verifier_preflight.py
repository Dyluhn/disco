"""Plan-verifier preflight for the finish gate.

Sole owner of "does an unchanged, still-failing plan-verifier fingerprint
block the optional model-owned finish verifier" policy. Extracted from
``_FinalizeMixin._preflight_failed_plan_verifier`` in ``finalize.py``, which
now delegates here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ... import signals
from ...stuck import successful_mutation_with_receipt
from ..common import ActionEvent, Event, ObservationEvent, StatusEvent, predicate_fingerprints


def _plan_predicate_fingerprints(events: list[Event]) -> list[str] | None:
    """Fingerprints of the current approved plan's done-condition predicates.

    None when there is no approved plan, or the plan carries no predicates
    to check.
    """
    plan = signals.latest_approved_plan(events)
    if plan is None:
        return None
    predicates = [step.done_condition for step in plan.steps if step.done_condition is not None]
    if not predicates:
        return None
    return predicate_fingerprints(predicates)


def _latest_matching_failure_seq(events: list[Event], predicate_fps: list[str]) -> int | None:
    """seq of the most recent ``plan_verifier_failure`` for this predicate set."""
    for ev in reversed(events):
        if isinstance(ev, StatusEvent) and ev.plan_verifier_failure is not None:
            if ev.plan_verifier_failure.predicate_fingerprints == predicate_fps:
                return ev.seq
    return None


def _productive_mutation_after(events: list[Event], since_seq: int) -> bool:
    """True when a successful, receipted mutation followed ``since_seq``."""
    action_by_id = {
        ev.id: ev for ev in events if isinstance(ev, ActionEvent) and ev.tool_call is not None
    }
    for ev in events:
        if (ev.seq or 0) <= since_seq or not isinstance(ev, ObservationEvent):
            continue
        action = action_by_id.get(ev.action_id) if ev.action_id is not None else None
        if successful_mutation_with_receipt(ev, action):
            return True
    return False


async def preflight_failed_plan_verifier(
    events: list[Event],
    finish_dod_gate_passed: Callable[[], Awaitable[bool]],
) -> bool:
    """Recheck an unchanged failing plan gate before optional verification.

    Find the currently approved plan predicate fingerprints. If the same
    predicate set has a prior typed ``plan_verifier_failure`` and no
    successful trusted mutation receipt after the latest such failure, call
    ``finish_dod_gate_passed`` first. If it returns False, return True so
    the caller returns ``Disp.CONTINUE`` before the optional verifier.

    Returns True when the optional shell verifier must be skipped (the
    caller should return CONTINUE).
    """
    predicate_fps = _plan_predicate_fingerprints(events)
    if predicate_fps is None:
        return False

    failure_seq = _latest_matching_failure_seq(events, predicate_fps)
    if failure_seq is None:
        return False

    if _productive_mutation_after(events, failure_seq):
        return False

    if not await finish_dod_gate_passed():
        return True

    return False

"""Typed plan-verifier failure record construction.

`_ContentGateMixin._record_plan_verifier_failure` owns the *orchestration*
(emit the failure, message the model, decide repair vs. replan vs. STUCK) —
all of that needs `self._loop`. Everything upstream of that decision —
fingerprinting the failure, counting prior attempts against the event log,
and deciding whether replanning is exhausted — is pure event-log arithmetic
with no loop dependency, so it lives here as `build_plan_verifier_failure`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ....dod import predicate_fingerprint, predicate_fingerprints
from ....events import Event, PlanEvent, PlanVerifierFailure, StatusEvent


def build_plan_verifier_failure(
    plan: PlanEvent,
    verdict: Any,
    events: list[Event],
) -> tuple[PlanVerifierFailure, list[Any]]:
    """Return the typed failure record and the DoD results that failed."""

    predicate_fps = predicate_fingerprints(
        [step.done_condition for step in plan.steps if step.done_condition is not None]
    )
    failed_results = [result for result in verdict.results if not result.passed]
    failure_kinds = [_plan_verifier_failure_kind(result) for result in failed_results]
    failure_fingerprint = _plan_verifier_failure_fingerprint(failed_results, failure_kinds)
    prior_same_plan = _prior_same_plan_failures(events, plan.id)
    approvals_with_same_predicates = _approvals_with_same_predicates(events, predicate_fps)
    prior_same_set_failures = _prior_same_predicate_set_failures(events, predicate_fps)
    replan_exhausted = approvals_with_same_predicates >= 2 and prior_same_set_failures + 1 >= 3
    failure = PlanVerifierFailure(
        plan_revision=plan.revision,
        plan_event_id=plan.id,
        predicate_fingerprints=predicate_fps,
        failed_predicate_fingerprints=_failed_predicate_fingerprints(failed_results),
        spec_fingerprint=verdict.spec_fingerprint,
        failure_fingerprint=failure_fingerprint,
        failure_kinds=failure_kinds,
        attempt_for_approved_plan=len(prior_same_plan) + 1,
        approvals_with_same_predicates=max(1, approvals_with_same_predicates),
        replan_allowed=not replan_exhausted,
    )
    return failure, failed_results


def _failed_predicate_fingerprints(results: list[Any]) -> list[str]:
    return [predicate_fingerprint(result.predicate) for result in results]


def _plan_verifier_failure_kind(result: Any) -> str:
    if bool(result.details.get("timed_out")):
        return "timeout"
    if bool(result.details.get("denied")):
        return "denied"
    if result.unverifiable:
        return "unverifiable"
    return str(result.details.get("kind") or result.predicate.kind)


def _plan_verifier_failure_fingerprint(failed_results: list[Any], failure_kinds: list[str]) -> str:
    failure_payload = [
        {
            "predicate": predicate_fingerprint(result.predicate),
            "kind": failure_kinds[index],
            "reason": result.reason,
            "exit_code": result.details.get("exit_code"),
            "status_code": result.details.get("status_code"),
            "timed_out": bool(result.details.get("timed_out")),
        }
        for index, result in enumerate(failed_results)
    ]
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(failure_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _prior_same_plan_failures(events: list[Event], plan_event_id: str) -> list[PlanVerifierFailure]:
    return [
        event.plan_verifier_failure
        for event in events
        if isinstance(event, StatusEvent)
        and event.plan_verifier_failure is not None
        and event.plan_verifier_failure.plan_event_id == plan_event_id
    ]


def _approvals_with_same_predicates(events: list[Event], predicate_fps: list[str]) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, StatusEvent)
        and event.plan_verification_transition is not None
        and event.plan_verification_transition.new_predicate_fingerprints == predicate_fps
    )


def _prior_same_predicate_set_failures(events: list[Event], predicate_fps: list[str]) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, StatusEvent)
        and event.plan_verifier_failure is not None
        and event.plan_verifier_failure.predicate_fingerprints == predicate_fps
    )

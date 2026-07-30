"""Durable repair-detection authority for typed plan-predicate replacement.

Extracted from ``plan_revisions.py`` as bounded pure collaborators over the
append-only event log.  These helpers answer one question each: whether a
failed verifier or a demonstrated command failure authorizes a same-width,
same-kind typed replacement through the approval boundary.

One implementation owner (this module); ``plan_revisions.py`` re-exports the
public names as the compatibility surface.  No duplicated policy.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    PlanPredicateDiff,
    predicate_fingerprint,
    predicate_fingerprints,
)
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from . import signals

if TYPE_CHECKING:
    pass


def plan_predicates(plan: PlanEvent | None) -> list[DoDPredicate]:
    if plan is None:
        return []
    return [step.done_condition for step in plan.steps if step.done_condition is not None]


def predicate_diff_for_revision(
    events: list[Event], candidate: PlanEvent
) -> PlanPredicateDiff | None:
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return None
    from ..dod import plan_predicate_diff

    return plan_predicate_diff(plan_predicates(approved), plan_predicates(candidate))


def _same_width_typed_replacement(events: list[Event], candidate: PlanEvent) -> bool:
    """A verifier repair may replace, but never drain, a failed typed set.

    File moves should normally use ``renamed_from`` and remain monotonic.  This
    narrow escape preserves the existing twice-failed command/http repair path:
    every old and new predicate remains visible in the typed approval
    transition, the causal repair classifier audits why replacement is allowed,
    and an 8→0 (or any cardinality-reducing) weakening remains impossible.
    """
    approved = signals.latest_approved_plan(events)
    old = plan_predicates(approved)
    new = plan_predicates(candidate)
    return bool(old) and len(old) == len(new)


def _twice_failed_verifier_seq(events: list[Event], approved: PlanEvent) -> int:
    """Seq of the latest twice-failed verifier failure for ``approved``."""
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent)
            and event.plan_verifier_failure is not None
            and event.plan_verifier_failure.plan_event_id == approved.id
            and event.plan_verifier_failure.plan_revision == approved.revision
            and event.plan_verifier_failure.attempt_for_approved_plan >= 2
        ),
        default=0,
    )


def _latest_approval_seq_before(events: list[Event], failure_seq: int) -> int:
    """Seq of the latest ``plan_approved`` strictly before ``failure_seq``."""
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == "plan_approved"
            and (event.seq or 0) < failure_seq
        ),
        default=0,
    )


def _repair_has_prior_productive_action(
    events: list[Event],
    failure_event: StatusEvent | None = None,
) -> bool:
    """Mirror the persisted repair classifier's productive-work prerequisite."""
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return False
    if failure_event is not None:
        failure_seq = failure_event.seq or 0
    else:
        failure_seq = _twice_failed_verifier_seq(events, approved)
    approval_seq = _latest_approval_seq_before(events, failure_seq)
    successful = signals.successful_action_ids(events)
    return bool(approval_seq and failure_seq) and any(
        approval_seq < (event.seq or 0) < failure_seq
        and signals.is_successful_productive_action(event, successful)
        for event in events
    )


def _persisted_candidate_seq(events: list[Event], candidate: PlanEvent) -> int:
    persisted = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, PlanEvent)
            and event.id == candidate.id
            and event.revision == candidate.revision
        ),
        None,
    )
    if persisted is not None:
        return persisted.seq or 0
    return max((event.seq or 0 for event in events), default=0) + 1


def _repairable_failure_candidates(
    events: list[Event],
    approved: PlanEvent,
    candidate_seq: int,
    approved_fingerprints: list[str],
) -> list[StatusEvent]:
    """Collect plan-owned, replan-allowed verifier failures before ``candidate_seq``."""
    return [
        event
        for event in events
        if isinstance(event, StatusEvent)
        and event.plan_verifier_failure is not None
        and (event.seq or 0) < candidate_seq
        and event.plan_verifier_failure.plan_event_id == approved.id
        and event.plan_verifier_failure.plan_revision == approved.revision
        and event.plan_verifier_failure.predicate_fingerprints == approved_fingerprints
        and event.plan_verifier_failure.replan_allowed
    ]


def _user_message_between(
    events: list[Event], after_seq: int, before_seq: int
) -> bool:
    """True when a USER message exists strictly between two seqs."""
    return any(
        isinstance(event, MessageEvent)
        and event.source == EventSource.USER
        and after_seq < (event.seq or 0) < before_seq
        for event in events
    )


def _latest_repairable_failure(events: list[Event], candidate: PlanEvent) -> StatusEvent | None:
    """Return the causal plan-owned failure immediately authorizing a correction.

    A single real verifier failure may justify proposing a changed typed verifier
    for approval.  That proposal authority is intentionally weaker than the
    twice-failed execution-bypass receipt in :func:`signals.plan_is_verifier_repair`.
    """
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return None
    candidate_seq = _persisted_candidate_seq(events, candidate)
    approved_fingerprints = predicate_fingerprints(plan_predicates(approved))
    candidates = _repairable_failure_candidates(
        events, approved, candidate_seq, approved_fingerprints
    )
    if not candidates:
        return None
    failure_event = max(candidates, key=lambda event: event.seq or 0)
    failure_seq = failure_event.seq or 0
    if _user_message_between(events, failure_seq, candidate_seq):
        return None
    return failure_event


def _diff_is_same_width_same_kind(diff: PlanPredicateDiff) -> bool:
    """True when added/dropped are equal-length and kind-for-kind."""
    if len(diff.added) != len(diff.dropped):
        return False
    return Counter(p.kind for p in diff.added) == Counter(p.kind for p in diff.dropped)


def failed_verifier_replacement_allowed(events: list[Event], candidate: PlanEvent) -> bool:
    """Allow one exact, failure-scoped typed replacement through approval.

    Every unaffected predicate must remain byte-identical (or be an ordinary
    audited file rename).  Each non-monotonic drop must be one of the exact
    failed predicate fingerprints, and it must be replaced one-for-one by the
    same predicate kind.  This lets a host-specific command or URL verifier be
    corrected after its first durable failure without granting the stricter
    twice-failed execution bypass or allowing an unrelated acceptance bar to
    disappear.
    """
    failure_event = _latest_repairable_failure(events, candidate)
    if failure_event is None or failure_event.plan_verifier_failure is None:
        return False
    diff = predicate_diff_for_revision(events, candidate)
    if diff is None or not diff.dropped or diff.invalid_renames:
        return False
    failed = Counter(failure_event.plan_verifier_failure.failed_predicate_fingerprints)
    dropped = Counter(predicate_fingerprint(predicate) for predicate in diff.dropped)
    if dropped - failed:
        return False
    if not _diff_is_same_width_same_kind(diff):
        return False
    return _repair_has_prior_productive_action(events, failure_event)


def _failed_action_ids(events: list[Event]) -> set[str]:
    """Build the set of action ids that have a failed terminal event."""
    failed: set[str] = set()
    for event in events:
        if isinstance(event, AgentErrorEvent):
            if isinstance(event.action_id, str):
                failed.add(event.action_id)
        elif isinstance(event, ObservationEvent):
            result = event.tool_result
            if getattr(result, "success", True) is False and isinstance(event.action_id, str):
                failed.add(event.action_id)
    return failed


def _action_has_matching_command(action: ActionEvent, normalized: str) -> bool:
    """True when any string argument of ``action`` matches ``normalized``."""
    arguments = getattr(action.tool_call, "arguments", None)
    if not isinstance(arguments, dict):
        return False
    return any(
        isinstance(value, str) and " ".join(value.split()) == normalized
        for value in arguments.values()
    )


def _agent_demonstrated_command_failure(events: list[Event], cmd: str) -> int | None:
    """Did the agent itself RUN this exact predicate command and see it fail?

    A command acceptance condition is authored by the agent, so it can be wrong —
    and the agent usually discovers that by executing it, not by waiting for the
    plan verifier. Both existing replacement escapes key on a
    `plan_verifier_failure`, so a predicate the verifier never ran had NO legal
    repair path: the agent held a condition it had proven impossible and every
    revision to correct it was refused as weakening.

    That is a deadlock, not a safeguard. Counted seed 440028 authored
    `python3 -c "…async def main(): async with …"` as a done-condition — invalid
    Python that cannot parse — ran it, got a SyntaxError, wrote a working
    `verify_app.py`, proved both required strings were in the rendered DOM, and
    was then refused SIXTEEN times when it tried to swap the broken condition for
    the working one. It re-ran its passing verification to demonstrate doneness
    until the thrash detector stopped the run.

    Direct execution is at least as strong as verifier evidence — it is the same
    command, run for real. Recognising it unlocks nothing else: the replacement
    must still be same-width, same-kind, one-for-one, and preceded by productive
    work. Only the source of the failure evidence widens.
    """

    normalized = " ".join(cmd.split())
    if not normalized:
        return None
    failed_action_ids = _failed_action_ids(events)
    latest: int | None = None
    for event in events:
        if not isinstance(event, ActionEvent) or event.id not in failed_action_ids:
            continue
        if _action_has_matching_command(event, normalized):
            latest = max(latest or 0, event.seq or 0)
    return latest


def _demonstrated_command_proof_seqs(
    events: list[Event], diff: PlanPredicateDiff
) -> list[int] | None:
    """Proof seqs for every dropped command predicate, or None if any unproven."""
    proof_seqs: list[int] = []
    for predicate in diff.dropped:
        if not isinstance(predicate, CommandExitPredicate):
            return None
        seq = _agent_demonstrated_command_failure(events, predicate.cmd)
        if seq is None:
            return None
        proof_seqs.append(seq)
    return proof_seqs


def _latest_plan_approved_seq(events: list[Event]) -> int:
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        default=0,
    )


def demonstrated_command_replacement_allowed(events: list[Event], candidate: PlanEvent) -> bool:
    """Same-width, same-kind replacement of a command predicate the agent PROVED
    is broken by running it. See `_agent_demonstrated_command_failure`."""

    diff = predicate_diff_for_revision(events, candidate)
    if diff is None or not diff.dropped or diff.invalid_renames:
        return False
    if not _diff_is_same_width_same_kind(diff):
        return False
    # EVERY dropped predicate must be a command the agent demonstrably ran and
    # saw fail — one unproven drop and the whole revision is still weakening.
    proof_seqs = _demonstrated_command_proof_seqs(events, diff)
    if proof_seqs is None:
        return False
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return False
    approval_seq = _latest_plan_approved_seq(events)
    earliest_proof = min(proof_seqs)
    if not approval_seq or earliest_proof <= approval_seq:
        return False
    # The repair must be EARNED: real work after proving the condition broken,
    # not an immediate swap. Mirrors the verifier path's productive-work fence,
    # fenced on the demonstrated failure instead of a verifier failure.
    successful = signals.successful_action_ids(events)
    return any(
        (event.seq or 0) > earliest_proof
        and signals.is_successful_productive_action(event, successful)
        for event in events
    )


def verifier_repair_replacement_allowed(events: list[Event], candidate: PlanEvent) -> bool:
    """Proposal-time form of the narrow, causal typed-repair escape."""
    return failed_verifier_replacement_allowed(events, candidate) or (
        signals.verifier_repair_planning_active(events)
        and _repair_has_prior_productive_action(events)
        and _same_width_typed_replacement(events, candidate)
    )

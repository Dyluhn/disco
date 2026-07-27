"""Durable authority rules for replacing an approved execution plan.

The helpers here are deliberately pure over the append-only event log.  They
provide one exact execution-contract identity, one monotonic predicate diff,
and one causal-input boundary shared by both revision entry paths.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    PlanPredicateDiff,
    idempotence_fingerprint,
    plan_predicate_diff,
    predicate_fingerprint,
    predicate_fingerprints,
)
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from . import signals
from .control import Disp

if TYPE_CHECKING:
    from .engine import AgentLoop

IDEMPOTENT_PLAN_DETAIL = "plan_revision_idempotent"
IDEMPOTENT_PLAN_DIAGNOSTIC = "identical_plan_redirect"
PLAN_WEAKENING_DETAIL = "plan_predicate_weakening_blocked"
PLAN_WEAKENING_BLOCKER = "plan_predicate_weakening"


class PlanRevisionWeakeningError(ValueError):
    """Raised when an approval backstop detects an unaudited predicate drop."""

    def __init__(self, diff: PlanPredicateDiff) -> None:
        self.diff = diff
        super().__init__(weakening_guidance(diff))


def plan_predicates(plan: PlanEvent | None) -> list[DoDPredicate]:
    if plan is None:
        return []
    return [step.done_condition for step in plan.steps if step.done_condition is not None]


def _dependency_identity(step: object) -> str | None:
    """Exact dependency slot for current/future PlanStep schemas.

    The current schema expresses dependency by ordered position and has no
    explicit field, so this returns ``None`` today.  If a versioned PlanStep
    later adds ``dependencies``/``depends_on``, duplicate suppression will
    immediately include its canonical JSON value rather than overlooking it.
    """
    value: Any = getattr(step, "dependencies", getattr(step, "depends_on", None))
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def normalized_execution_contract(
    plan: PlanEvent,
) -> tuple[tuple[str, str | None, str | None, str | None], ...]:
    """Exact ordered title/detail/predicate/dependency execution identity."""
    return tuple(
        (
            step.title,
            step.detail,
            idempotence_fingerprint(step.done_condition)
            if step.done_condition is not None
            else None,
            _dependency_identity(step),
        )
        for step in plan.steps
    )


def _latest_approval_seq(events: list[Event]) -> int | None:
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        default=None,
    )


def has_new_plan_cause_since_approval(events: list[Event]) -> bool:
    """Whether durable user/failure/work evidence justifies another approval."""
    approval_seq = _latest_approval_seq(events)
    if approval_seq is None:
        return True
    successful_actions = signals.successful_action_ids(events)
    for event in events:
        if (event.seq or 0) <= approval_seq:
            continue
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            return True
        if isinstance(event, StatusEvent) and event.plan_verifier_failure is not None:
            return True
        if signals.is_successful_productive_action(event, successful_actions):
            return True
    return False


def is_idempotent_plan_revision(events: list[Event], candidate: PlanEvent) -> bool:
    """True only for an exact causeless re-submission of current authority."""
    approved = signals.latest_approved_plan(events)
    return (
        approved is not None
        and normalized_execution_contract(approved) == normalized_execution_contract(candidate)
        and not has_new_plan_cause_since_approval(events)
    )


def predicate_diff_for_revision(
    events: list[Event], candidate: PlanEvent
) -> PlanPredicateDiff | None:
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return None
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


def _repair_has_prior_productive_action(
    events: list[Event],
    failure_event: StatusEvent | None = None,
) -> bool:
    """Mirror the persisted repair classifier's productive-work prerequisite."""
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return False
    failure_seq = (failure_event.seq or 0) if failure_event is not None else 0
    if failure_event is None:
        failure_seq = max(
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
    approval_seq = max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == "plan_approved"
            and (event.seq or 0) < failure_seq
        ),
        default=0,
    )
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
    failure_event = max(
        (
            event
            for event in events
            if isinstance(event, StatusEvent)
            and event.plan_verifier_failure is not None
            and (event.seq or 0) < candidate_seq
            and event.plan_verifier_failure.plan_event_id == approved.id
            and event.plan_verifier_failure.plan_revision == approved.revision
            and event.plan_verifier_failure.predicate_fingerprints == approved_fingerprints
            and event.plan_verifier_failure.replan_allowed
        ),
        key=lambda event: event.seq or 0,
        default=None,
    )
    if failure_event is None:
        return None
    failure_seq = failure_event.seq or 0
    if any(
        isinstance(event, MessageEvent)
        and event.source == EventSource.USER
        and failure_seq < (event.seq or 0) < candidate_seq
        for event in events
    ):
        return None
    return failure_event


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
    if len(diff.added) != len(diff.dropped):
        return False
    if Counter(predicate.kind for predicate in diff.added) != Counter(
        predicate.kind for predicate in diff.dropped
    ):
        return False
    return _repair_has_prior_productive_action(events, failure_event)


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
    failed_action_ids: set[str] = set()
    for event in events:
        if isinstance(event, AgentErrorEvent):
            if isinstance(event.action_id, str):
                failed_action_ids.add(event.action_id)
        elif isinstance(event, ObservationEvent):
            result = event.tool_result
            if getattr(result, "success", True) is False and isinstance(event.action_id, str):
                failed_action_ids.add(event.action_id)
    latest: int | None = None
    for event in events:
        if not isinstance(event, ActionEvent) or event.id not in failed_action_ids:
            continue
        arguments = getattr(event.tool_call, "arguments", None)
        if not isinstance(arguments, dict):
            continue
        for value in arguments.values():
            if isinstance(value, str) and " ".join(value.split()) == normalized:
                latest = max(latest or 0, event.seq or 0)
    return latest


def demonstrated_command_replacement_allowed(events: list[Event], candidate: PlanEvent) -> bool:
    """Same-width, same-kind replacement of a command predicate the agent PROVED
    is broken by running it. See `_agent_demonstrated_command_failure`."""

    diff = predicate_diff_for_revision(events, candidate)
    if diff is None or not diff.dropped or diff.invalid_renames:
        return False
    if len(diff.added) != len(diff.dropped):
        return False
    if Counter(predicate.kind for predicate in diff.added) != Counter(
        predicate.kind for predicate in diff.dropped
    ):
        return False
    # EVERY dropped predicate must be a command the agent demonstrably ran and
    # saw fail — one unproven drop and the whole revision is still weakening.
    proof_seqs: list[int] = []
    for predicate in diff.dropped:
        if not isinstance(predicate, CommandExitPredicate):
            return False
        seq = _agent_demonstrated_command_failure(events, predicate.cmd)
        if seq is None:
            return False
        proof_seqs.append(seq)
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return False
    approval_seq = max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        default=0,
    )
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


async def preflight_plan_revision(
    loop: AgentLoop,
    candidate: PlanEvent,
    events: list[Event],
) -> Disp | None:
    """Shared pre-persistence idempotence and weakening boundary."""
    if is_idempotent_plan_revision(events, candidate):
        return await redirect_idempotent_revision(loop, candidate, events)
    diff = predicate_diff_for_revision(events, candidate)
    if (
        diff is not None
        and not diff.is_monotonic
        and not verifier_repair_replacement_allowed(events, candidate)
    ):
        return await reject_plan_weakening(loop, candidate, diff)
    return None


def user_steer_authorizes_weakening(events: list[Event]) -> bool:
    """A durable USER steer since the last approval IS the owner's weakening decision.

    The weakening guard refuses a revision that drops an approved acceptance
    condition and tells the agent that "removing an acceptance condition requires
    a separate explicit owner weakening decision". That is the right invariant —
    an agent must never silently lower its own bar — but until now there was no
    way for the agent to OBTAIN such a decision, so the refusal named a rule
    without naming an achievable action.

    Counted-promotion failure 2026-07-27 (`p4_ff_react_steer` seed 700022,
    FALSE_FINISH_NO_OUTPUT). A mid-run user steer changed the build's direction,
    which legitimately obsoleted `file_exists:package.json`,
    `file_exists:src/App.tsx` and `command:test -f dist/index.html`. Every
    revision that dropped them was blocked — SIXTEEN times, driving 29 planning
    turns against an all-time observed maximum of 14 — until the agent gave up
    and carried `package.json` into a plan whose new direction never produces it.
    The run was then guaranteed to fail the output-truth gate whatever it did.

    The steer WAS the owner decision the refusal demanded. `revision_steer_pending`
    is appended by the kernel ingress (`DiscoKernel.send_user_turn`), never by the
    model, so it is host-authored authority the agent cannot forge.

    Deliberately narrow: the marker must post-date the latest `plan_approved`, so
    this authorizes exactly one revision cycle per steer. A spontaneous
    agent-initiated drop with no steer behind it is still blocked, and a generic
    plan approval still cannot remove a condition.
    """
    for event in reversed(events):
        if not isinstance(event, StatusEvent):
            continue
        if event.detail == "plan_approved":
            return False  # reached the approval without finding a newer steer
        if event.detail == "revision_steer_pending":
            return True
    return False


def assert_plan_revision_approvable(events: list[Event], candidate: PlanEvent) -> None:
    """Approval-funnel backstop for every persisted revision route."""
    diff = predicate_diff_for_revision(events, candidate)
    if (
        diff is None
        or diff.is_monotonic
        or user_steer_authorizes_weakening(events)
        or failed_verifier_replacement_allowed(events, candidate)
        or demonstrated_command_replacement_allowed(events, candidate)
        or (
            _same_width_typed_replacement(events, candidate)
            and signals.plan_is_verifier_repair(events, candidate)
        )
    ):
        return
    raise PlanRevisionWeakeningError(diff)


def predicate_label(predicate: DoDPredicate) -> str:
    if isinstance(predicate, FileExistsPredicate):
        return f"file_exists:{predicate.path}"
    if isinstance(predicate, CommandExitPredicate):
        return f"command:{predicate.cmd}"
    if isinstance(predicate, HTTPOkPredicate):
        return f"http_ok:{predicate.url}"
    return str(predicate)


def weakening_guidance(diff: PlanPredicateDiff) -> str:
    dropped = ", ".join(predicate_label(predicate) for predicate in diff.dropped) or "none"
    forged = (
        ", ".join(
            f"{predicate.path} renamed_from {predicate.renamed_from}"
            for predicate in diff.invalid_renames
        )
        or "none"
    )
    return (
        "The revised plan was NOT accepted because it would silently weaken the "
        f"approved plan-owned verification contract. Dropped: {dropped}. Invalid "
        f"renames: {forged}. Retain each condition, or move a file condition by "
        "putting renamed_from on its replacement. Removing an acceptance condition "
        "requires a separate explicit owner weakening decision; a generic plan "
        "approval cannot remove it."
    )


def idempotent_execution_guidance(events: list[Event]) -> str:
    plan, states = signals.effective_plan_progress(events)
    next_step = "the next incomplete approved step"
    contract = ""
    if plan is not None:
        contract = f"\n\nCurrent approved execution contract:\n{plan.to_llm_message().content}"
        for index, step in enumerate(plan.steps, start=1):
            if states.get(index) != "done":
                next_step = step.title
                break
    return (
        "This execution contract is exactly the currently approved plan and no new "
        "user instruction, verifier failure, or productive work justifies another "
        "revision. No new plan or approval gate was created. Continue execution now "
        f"with: {next_step}.{contract}"
    )


async def redirect_idempotent_revision(
    loop: AgentLoop,
    candidate: PlanEvent,
    events: list[Event],
) -> Disp:
    """Persist the durable execution boundary and paired model guidance only."""
    loop._planner.discard_plan_predicates(candidate.revision)
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail=IDEMPOTENT_PLAN_DETAIL,
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"{idempotent_execution_guidance(events)}\n"
                    "</system-reminder>"
                ),
            ),
            meta={"diagnostic": IDEMPOTENT_PLAN_DIAGNOSTIC},
        )
    )
    loop._identical_plan_revisions = 0
    loop.mode = loop._execution_mode
    return Disp.CONTINUE


async def reject_plan_weakening(
    loop: AgentLoop,
    candidate: PlanEvent,
    diff: PlanPredicateDiff,
) -> Disp:
    """Recoverably reject a silent acceptance-bar drop without a STUCK path."""
    loop._planner.discard_plan_predicates(candidate.revision)
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail=PLAN_WEAKENING_DETAIL,
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=f"<system-reminder>\n{weakening_guidance(diff)}\n</system-reminder>",
            ),
            meta={"blocking": PLAN_WEAKENING_BLOCKER},
        )
    )
    return Disp.CONTINUE


async def reject_invalid_revision_conditions(
    loop: AgentLoop,
    candidate: PlanEvent,
    errors: list[str],
) -> Disp:
    """Reject malformed revision predicates before lenient coercion can hide them."""
    loop._planner.discard_plan_predicates(candidate.revision)
    rendered = "\n".join(f"- {error}" for error in errors)
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="invalid_plan_done_conditions",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe revised plan was NOT accepted because "
                    "its done_condition values are malformed, unsafe, or incompatible "
                    f"with the active build profile:\n{rendered}\nCorrect the typed "
                    "conditions and propose the revision again; no condition was "
                    "silently discarded.\n</system-reminder>"
                ),
            ),
            meta={"blocking": "invalid_plan_done_conditions"},
        )
    )
    return Disp.CONTINUE


async def route_plan_approval_gate(loop: AgentLoop, plan: PlanEvent) -> Disp:
    """Route a persisted candidate through one guarded approval boundary."""
    events = await loop._events()
    try:
        assert_plan_revision_approvable(events, plan)
    except PlanRevisionWeakeningError as exc:
        return await reject_plan_weakening(loop, plan, exc.diff)
    if loop._autonomous:
        await loop._emit(await loop._plan_approval_status(plan, events))
        loop.mode = loop._execution_mode
        await loop._seed_context_from_plan()
        return Disp.CONTINUE
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            detail=plan.id,
        )
    )
    return Disp.HALT

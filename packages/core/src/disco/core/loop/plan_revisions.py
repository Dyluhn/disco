"""Durable authority rules for replacing an approved execution plan.

The helpers here are deliberately pure over the append-only event log.  They
provide one exact execution-contract identity, one monotonic predicate diff,
and one causal-input boundary shared by both revision entry paths.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    PlanPredicateDiff,
    idempotence_fingerprint,
    plan_predicate_diff,
)
from ..events import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
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


def _repair_has_prior_productive_action(events: list[Event]) -> bool:
    """Mirror the persisted repair classifier's productive-work prerequisite."""
    approved = signals.latest_approved_plan(events)
    if approved is None:
        return False
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


def verifier_repair_replacement_allowed(events: list[Event], candidate: PlanEvent) -> bool:
    """Proposal-time form of the narrow, causal typed-repair escape."""
    return (
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


def assert_plan_revision_approvable(events: list[Event], candidate: PlanEvent) -> None:
    """Approval-funnel backstop for every persisted revision route."""
    diff = predicate_diff_for_revision(events, candidate)
    if (
        diff is None
        or diff.is_monotonic
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

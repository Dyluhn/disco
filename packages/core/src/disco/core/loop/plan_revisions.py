"""Durable authority rules for replacing an approved execution plan.

The helpers here are deliberately pure over the append-only event log.  They
provide one exact execution-contract identity, one monotonic predicate diff,
and one causal-input boundary shared by both revision entry paths.

Repair-detection collaborators (failed-verifier and demonstrated-command
replacement) live in :mod:`plan_repair_detection` and are re-exported here as
the compatibility surface.  One implementation owner; no duplicated policy.
"""

from __future__ import annotations

import json
from collections import Counter  # noqa: F401 — compatibility facade binding
from typing import TYPE_CHECKING, Any

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    PlanPredicateDiff,
    idempotence_fingerprint,
    plan_predicate_diff,  # noqa: F401 — compatibility facade binding
    predicate_fingerprint,  # noqa: F401 — compatibility facade binding
    predicate_fingerprints,  # noqa: F401 — compatibility facade binding
)
from ..events import (
    ActionEvent,  # noqa: F401 — compatibility facade binding
    AgentErrorEvent,  # noqa: F401 — compatibility facade binding
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,  # noqa: F401 — compatibility facade binding
    PlanEvent,
    StatusEvent,
    WorkspaceRestoredEvent,
)
from . import signals
from .control import Disp
from .plan_repair_detection import (  # noqa: F401 — re-exported for back-compat
    _agent_demonstrated_command_failure,
    _latest_repairable_failure,
    _persisted_candidate_seq,
    _repair_has_prior_productive_action,
    _same_width_typed_replacement,
    demonstrated_command_replacement_allowed,
    failed_verifier_replacement_allowed,
    plan_predicates,
    predicate_diff_for_revision,
    verifier_repair_replacement_allowed,
)

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


def workspace_restore_authorizes_weakening(events: list[Event]) -> bool:
    """A workspace RESTORE since the last approval is the same class of authority.

    Sibling of `user_steer_authorizes_weakening`, and found the same way. That one
    taught the guard that a user steer legitimately obsoletes the conditions it
    supersedes. A rollback obsoletes them at least as hard: the platform itself
    destroyed the state the condition describes, so the condition is not being
    lowered, it is being made unsatisfiable by an authority above the agent.

    Certified-lane evidence 2026-07-27 (`p4_ff_import_rollback` seed 900029, PASS
    at 36 planning turns against an all-time maximum of 8). An approved condition
    `command:grep -q 'First revision 900029' index.html` survived a
    `workspace_restored` that deleted the string it greps for. Every revision
    dropping it was refused — TWENTY-FIVE times, twenty-three of them
    byte-identical — because "removing an acceptance condition requires a
    separate explicit owner weakening decision", which an autonomous agent has no
    way to obtain. Corroborated by `p4_appkit_rollback` seed 900044 (18 planning
    turns vs max 12) blocking on a `file_exists:` condition, so it is not
    specific to one predicate type.

    Condition identities are keys — `command:{cmd}`, `file_exists:{path}` — and a
    key outlives the state it describes. That is precisely why the deadlock is
    total rather than merely awkward: no revision can satisfy the guard, so the
    loop ends only if the agent stumbles onto a plan it tolerates.

    `WorkspaceRestoredEvent` is emitted by the agent-server's workspace service
    and is documented as "a user-requested workspace rollback"; there is no
    agent-callable restore tool. So this is host-authored authority the model
    cannot forge, exactly like `revision_steer_pending`.

    Deliberately narrow, mirroring the steer route: the restore must post-date the
    latest `plan_approved`, authorizing exactly one revision cycle. A spontaneous
    drop with no restore behind it is still blocked, and a generic plan approval
    still cannot remove a condition.
    """
    for event in reversed(events):
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            return False  # reached the approval without finding a newer restore
        if isinstance(event, WorkspaceRestoredEvent):
            return True
    return False


def assert_plan_revision_approvable(events: list[Event], candidate: PlanEvent) -> None:
    """Approval-funnel backstop for every persisted revision route."""
    diff = predicate_diff_for_revision(events, candidate)
    if (
        diff is None
        or diff.is_monotonic
        or user_steer_authorizes_weakening(events)
        or workspace_restore_authorizes_weakening(events)
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

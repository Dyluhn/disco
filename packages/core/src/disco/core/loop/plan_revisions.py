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
from typing import TYPE_CHECKING, Any, Protocol

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
from .ordinals import ordinal
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
    from .ports import ConversationModePort, GateCounterPort, LoopEventPort, PlanLifecyclePort

    class _LoopFacet(

        ConversationModePort,

        GateCounterPort,

        LoopEventPort,

        PlanLifecyclePort,

        Protocol,

    ):
        """The loop capability this module uses: conversation mode, gate counters, the event log,
        the plan lifecycle.
        """

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
    loop: _LoopFacet,
    candidate: PlanEvent,
    events: list[Event],
) -> Disp | None:
    """Shared pre-persistence idempotence boundary.

    Model-authored done conditions are advisory, so replacing or dropping one is
    never an acceptance-bar weakening. External DoD and admitted target contracts
    live outside the plan and are unaffected by this boundary.
    """
    if is_idempotent_plan_revision(events, candidate):
        return await redirect_idempotent_revision(loop, candidate, events)
    return None


def user_steer_authorizes_weakening(events: list[Event]) -> bool:
    """Return the historical post-approval steer signal.

    Kept for log analysis and compatibility. Model-authored plan conditions are
    advisory now, so plan approval no longer depends on this signal.
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
    """Return the historical post-approval workspace-restore signal.

    Kept for log analysis and compatibility. Model-authored plan conditions are
    advisory now, so plan approval no longer depends on this signal.
    """
    for event in reversed(events):
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            return False  # reached the approval without finding a newer restore
        if isinstance(event, WorkspaceRestoredEvent):
            return True
    return False


def assert_plan_revision_approvable(events: list[Event], candidate: PlanEvent) -> None:
    """Compatibility backstop; advisory plan checks cannot weaken acceptance.

    The arguments remain part of the public compatibility surface. Approval-time
    shape and safety validation happens before this function; hard acceptance is
    owned by external DoD and admitted target contracts.
    """
    del events, candidate


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
    loop: _LoopFacet,
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


async def _weakening_refusals_so_far(loop: _LoopFacet) -> int:
    """How many times this refusal has already fired, plus this one.

    GROUNDED FEEDBACK constraint 4, F55. Counted off the DURABLE log via the
    `blocking` label the refusal already carried before this repair, so the
    escalation survives a restart or a condensation exactly as the run's own
    evidence does. Deliberately NOT an instance counter and NOT a window: the
    `_plan_nudge` defect repaired at 2026-08-07j was a correct renderer fed a
    resetting segment count, which is the anti-pattern this campaign keeps
    finding, and no new mechanism is invented where the existing one fits.
    """
    events = await loop._events()
    return 1 + sum(
        1
        for event in events
        if isinstance(event, MessageEvent) and event.meta.get("blocking") == PLAN_WEAKENING_BLOCKER
    )


def _weakening_escalation(repeats: int) -> str:
    """The repetition clause, or nothing on a first firing.

    At 1 this renders "" and the body is byte-identical to every weakening
    refusal written before this repair, so the 07j corpus's own first firings
    (`d2-r1` seq 109, `d3-r1` seq 31) still match by content.
    """
    if repeats <= 1:
        return ""
    return (
        f"\n\nThis is the {ordinal(repeats)} time this run has proposed a plan "
        f"revision that weakens the approved contract; the previous "
        f"{repeats - 1} were refused for the same reason and re-proposing it "
        "again will not approve it. Each attempt spends a turn toward the "
        "no-progress limit that ENDS this run. Restore the conditions listed "
        "above, or say plainly that you cannot meet them."
    )


async def reject_plan_weakening(
    loop: _LoopFacet,
    candidate: PlanEvent,
    diff: PlanPredicateDiff,
) -> Disp:
    """Recoverably reject a silent acceptance-bar drop without a STUCK path.

    F55 (2026-08-07l): `weakening_guidance` is a pure function of the predicate
    diff with no access to run history, and this seam rendered it verbatim — so
    a run that re-proposed the same weakening read the identical paragraph. The
    07j corpus caught it at 3x (`d2-r1/p4_ff_react_continue` seqs 109/115/121)
    and 2x (`d3-r1/p4_ff_python_cancel_recovery` seqs 31/34). That is F53's
    shape at a different pair of seams, and it is answered the same way: the
    count is read HERE, at the emitter, which is the only side of the pair that
    can see the log.
    """
    loop._planner.discard_plan_predicates(candidate.revision)
    # Read BEFORE this refusal's own event is emitted, so the count is
    # "prior fires + this one".
    repeats = await _weakening_refusals_so_far(loop)
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
                content=(
                    "<system-reminder>\n"
                    f"{weakening_guidance(diff)}"
                    f"{_weakening_escalation(repeats)}\n"
                    "</system-reminder>"
                ),
            ),
            meta={"blocking": PLAN_WEAKENING_BLOCKER},
        )
    )
    return Disp.CONTINUE


async def reject_invalid_revision_conditions(
    loop: _LoopFacet,
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


async def route_plan_approval_gate(loop: _LoopFacet, plan: PlanEvent) -> Disp:
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

"""Model-facing notice construction for the content/DoD finish gates.

Each function here builds the exact same `StatusEvent`/`MessageEvent` pair(s)
that used to be inlined in `_ContentGateMixin`, and emits them through the
passed-in `loop` — the loop is an explicit argument rather than an implicit
`self`, so these stay plain functions instead of new mixin members. Moving
this text out is a pure LOC/complexity extraction: no wording, ordering, or
`meta`/`detail` value changes from the original inline call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ....events import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanVerifierFailure,
    PlanVerifierPass,
    StatusEvent,
)
from ...ordinals import ordinal
from ...plan_conditions import DictatedContentCondition
from .errors import _DictatedContentInspectionIncomplete

if TYPE_CHECKING:
    from ...ports import GateCounterPort, LoopEventPort, TurnControlPort

    class _LoopFacet(GateCounterPort, LoopEventPort, TurnControlPort, Protocol):
        """The loop capability this module uses: gate counters, the event log, turn control."""


async def emit_dictated_content_inspection_incomplete_notice(
    loop: _LoopFacet,
    inspection_error: _DictatedContentInspectionIncomplete,
    inspection_cause: dict[str, str | int],
) -> None:
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="dictated_content_inspection_incomplete",
            meta={"inspection_cause": inspection_cause},
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe dictated-content finish check could not "
                    "inspect the selected app completely: "
                    f"{inspection_error}. The task is NOT "
                    "complete and has been paused fail-closed; repair the workspace "
                    "or sandbox evidence surface before finishing.\n</system-reminder>"
                ),
            ),
        )
    )


async def emit_dictated_content_cap_release_notice(
    loop: _LoopFacet,
    cond: DictatedContentCondition,
    file_word: str,
    files: str,
) -> None:
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="dictated_content_release",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished despite missing dictated content after "
                    f"{loop._dictated_content_refusals} refusals: "
                    f"literal {cond.literal!r} from plan revision {cond.revision} "
                    f"still was not found in deliverable {file_word} {files}. "
                    "Releasing the finish gate to avoid an unbounded loop; "
                    "the deliverable may fail content review."
                ),
            ),
        )
    )


async def emit_dictated_content_refusal_notice(
    loop: _LoopFacet,
    cond: DictatedContentCondition,
    file_word: str,
    files: str,
) -> None:
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "You called finish, but a quoted user literal is missing "
                    f"from the deliverable {file_word} {files}: {cond.literal!r}.\n\n"
                    f"This literal was dictated in the user instruction for plan "
                    f"revision {cond.revision} and is carried forward into the "
                    "current revision. The match is case-sensitive and exact. "
                    "It needs to appear in one appropriate text deliverable, not "
                    "in every listed file. Never add text to a binary asset. Update "
                    "a suitable text deliverable so it contains that exact text, then "
                    "finish again.\n"
                    "</system-reminder>"
                ),
            ),
            # Typed provenance, matching this gate's sibling rejections
            # (dod_unmet, plan_verification_evidence_invalid): a finish-gate
            # refusal is a NEW proof obligation, and recovery-episode
            # derivation must recognize it without parsing prose.
            meta={"blocking": "user_literal_missing"},
        )
    )


async def _notice_repeat(loop: _LoopFacet, blocking: str) -> int:
    """How many times this fail-closed notice has already fired, plus this one.

    GROUNDED FEEDBACK constraint 4. These notices pause the run fail-closed and
    can each fire again after the user resumes, so a static body meant an agent
    that had already failed to repair the condition read the identical paragraph.
    Counted off the DURABLE log via the `blocking` label the notices already
    carried, so the escalation survives a restart.
    """
    events = await loop._events()
    return 1 + sum(
        1
        for event in events
        if isinstance(event, MessageEvent) and event.meta.get("blocking") == blocking
    )


# The `what` clause the plan-verifier notice hands `_again`, given a name so a test
# can DERIVE it instead of retyping it (F61, 2026-08-07t; the Attestation-Binding
# Invariant, F58). Value verbatim from the call argument it replaces.
#
# ONLY this one is named, deliberately. `_again` takes three sibling clauses in this
# module — 'the plan evidence has been rejected', 'the sandbox evidence surface has
# been unavailable', 'the Definition-of-Done has been reported unmet' — which no test
# restates today and which F61's scope does not name. Naming them too would widen a
# scope by drift, which is what F58's own scope section exists to prevent. They are
# recorded as an F61-class residue instead: unnamed, so the gate is silent about them
# by construction, and available to a boundary commissioned for them.
_PLAN_VERIFICATION_CONDITIONS_FAILED = "the plan verification conditions have failed"


def _again(repeats: int, what: str) -> str:
    """The repetition clause, or nothing on a first firing.

    F56 (2026-08-07l): the ordinal was a naive `{n}th` and rendered "the 2th
    time" to a live agent in the 07j sealed corpus. Only the ordinal moved; the
    first-firing branch and the rest of the sentence are byte-identical, so
    pre-repair logs stay recognisable by content.
    """
    if repeats <= 1:
        return ""
    return (
        f" This is the {ordinal(repeats)} time {what} in this run; "
        f"the previous {repeats - 1} did not clear it."
    )


async def emit_dangling_plan_evidence_notice(loop: _LoopFacet) -> None:
    repeats = await _notice_repeat(loop, "plan_verification_evidence_invalid")
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_verification_evidence_invalid",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe approved-plan verification record "
                    "does not match a complete persisted PlanEvent. The task is "
                    "NOT complete and has been paused fail-closed; repair the "
                    "event evidence before continuing."
                    f"{_again(repeats, 'the plan evidence has been rejected')}"
                    "\n</system-reminder>"
                ),
            ),
            meta={"blocking": "plan_verification_evidence_invalid"},
        )
    )


async def emit_dod_workspace_unavailable_notice(loop: _LoopFacet) -> None:
    repeats = await _notice_repeat(loop, "dod_workspace_unavailable")
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe acceptance requirements "
                    "could not be evaluated because the sandbox evidence surface "
                    "is unavailable. The task is NOT complete and has been paused "
                    "fail-closed; repair the sandbox before continuing."
                    f"{_again(repeats, 'the sandbox evidence surface has been unavailable')}"
                    "\n</system-reminder>"
                ),
            ),
            meta={"blocking": "dod_workspace_unavailable"},
        )
    )


async def emit_plan_verifier_failure_notice(
    loop: _LoopFacet,
    plan: PlanEvent,
    failure: PlanVerifierFailure,
    failed_results: list[Any],
) -> None:
    # Constraint 4 (2026-08-07i). This notice carried the `blocking` label its
    # three siblings in this module count on, and did not count. It fired
    # byte-identically twice in TWO different cells of the 07h corpus
    # (`p4_ff_react_steer@99201` seqs 114/131, `p4_ff_python_cancel_recovery@99704`
    # seqs 71/74): a failing verifier re-run against an unchanged plan revision
    # renders an unchanged body BY CONSTRUCTION, so only a count breaks the tie.
    # The module's own durable helper fits — no second mechanism is introduced.
    repeats = await _notice_repeat(loop, "plan_verifier_failed")
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_verification_failed",
            plan_verifier_failure=failure,
        )
    )
    unmet_block = "\n".join(
        f"  - {result.predicate!r}\n      reason: {result.reason}" for result in failed_results
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe current approved plan's verification "
                    f"conditions failed (plan revision {plan.revision}, "
                    f"failure {failure.failure_fingerprint}):\n\n{unmet_block}\n\n"
                    "The task is NOT complete. These conditions belong only to the "
                    "current plan revision. Fix the deliverable and retry, or submit a "
                    "revised plan for approval; a newer approved plan may replace these "
                    "plan-owned conditions but cannot alter external acceptance requirements."
                    f"{_again(repeats, _PLAN_VERIFICATION_CONDITIONS_FAILED)}"
                    "\n</system-reminder>"
                ),
            ),
            meta={
                "blocking": "plan_verifier_failed",
                "failure_fingerprint": failure.failure_fingerprint,
            },
        )
    )


async def emit_plan_verifier_replan_required_notice(loop: _LoopFacet) -> None:
    events = await loop._events()
    failures = sum(
        1
        for event in events
        if isinstance(event, StatusEvent) and event.plan_verifier_failure is not None
    )
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="planning",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe same unchanged plan-owned verifier "
                    f"has failed {failures} times on this record. Re-planning is now "
                    "required. Submit a corrected plan; it must preserve every "
                    "external requirement.\n"
                    "</system-reminder>"
                ),
            ),
            meta={"blocking": "plan_verifier_replan_required"},
        )
    )


async def emit_plan_verification_passed_notice(
    loop: _LoopFacet,
    plan: PlanEvent,
    predicate_fps: list[str],
    spec_fingerprint: str,
) -> None:
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_verification_passed",
            plan_verifier_pass=PlanVerifierPass(
                plan_revision=plan.revision,
                plan_event_id=plan.id,
                predicate_fingerprints=predicate_fps,
                spec_fingerprint=spec_fingerprint,
            ),
        )
    )


async def emit_external_dod_cap_pause_notice(loop: _LoopFacet) -> None:
    repeats = await _notice_repeat(loop, "dod_unmet")
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nThe external Definition-of-Done is "
                    "still unmet after the bounded retry budget. The task is NOT "
                    "complete. This run is pausing for explicit user review rather "
                    "than silently marking incomplete work done."
                    f"{_again(repeats, 'the Definition-of-Done has been reported unmet')}"
                    "\n</system-reminder>"
                ),
            ),
            meta={"blocking": "dod_unmet"},
        )
    )


async def emit_external_dod_unmet_notice(loop: _LoopFacet, verdict: Any) -> None:
    unmet_lines: list[str] = []
    for result in verdict.results:
        if result.passed:
            continue
        # The frozen predicate's repr names kind + fields. Pair with
        # the verdict's reason (the human explanation).
        label = "could not verify" if result.unverifiable else "reason"
        unmet_lines.append(f"  - {result.predicate!r}\n      {label}: {result.reason}")
    unmet_block = "\n".join(unmet_lines) if unmet_lines else "  - (no per-predicate results)"
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "You called finish, but the external Definition-of-Done "
                    f"evaluator found {len(verdict.unmet)} unmet acceptance "
                    f"predicate(s) (spec fingerprint {verdict.spec_fingerprint}):\n\n"
                    f"{unmet_block}\n\n"
                    "The task is NOT complete. These predicates were captured at "
                    "an external authority and live outside the agent's tool surface — "
                    "a plan revision cannot edit, remove, or shadow them. Fix what they "
                    "surface (the predicates name the gap), then finish again.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )


async def land_execution_nudge_exhausted(loop: _LoopFacet, execution_nudges: int) -> None:
    await loop._land_blocked(
        reason="approve_plan_no_execution",
        guidance=(
            "Plan approved but no execution action was taken after "
            f"{execution_nudges} execution reminders. "
            "The plan was not executed."
        ),
        legacy_status=ConversationStatus.STUCK,
        legacy_detail="approve_plan_no_execution",
    )


async def emit_execution_nudge(loop: _LoopFacet, thought: str, nudge_text: str) -> bool:
    """Surface the model's reasoning (if any) then send the nudge.

    Returns True iff the thought was non-blank and got surfaced — the caller
    treats a False return as an "invisible step" for its own telemetry.
    """

    surfaced = bool(thought.strip())
    if surfaced:
        await loop._emit(
            MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=thought),
            )
        )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=nudge_text),
        )
    )
    return surfaced


"""Pure renderers for the `serve` refusal family (GROUNDED FEEDBACK constraint 4).

Extracted from :mod:`turn_control_support` at 2026-08-07b, and the reason is
worth recording: that module sat 4 logical lines under its 700-line budget cap,
so the twice-rule repair for the six `serve` refusal surfaces did not fit there.
The extraction is a size-gate decomposition in the repo's own idiom — same
package, same layer, pure functions, no new behaviour and no new seam. Every
function here is a projection of arguments the caller already holds; the repeat
counts are read from the durable log by the caller (`_prior_diagnostic_count`)
and passed in, so this module has no import back into its parent.
"""

from __future__ import annotations

from ..events import Event, PlanEvent
from .boundaries import AgentStep


def serve_spend_escalation(repeats: int, why: str, body: str) -> str:
    """Wrap a serve refusal in its repeat count and what repeating costs.

    Shared by both serve refusal families: `serve` is not counted as work, so
    every refused attempt spends one turn toward the no-progress limit that ends
    the run. Below the second firing the body stands alone.
    """
    if repeats <= 1:
        return body
    return (
        "<system-reminder>\n"
        f"STOP — `serve` has now been refused {repeats} times in this run {why}.\n"
        f"{body}\n"
        "`serve` is not counted as work, so each refused attempt spends one turn "
        "toward the no-progress limit that ENDS this run.\n"
        "</system-reminder>"
    )


# The `serve` argument defects, and the ONE corrective move each admits. Held as
# ONE owner keyed by defect (the F47 single-owner precedent) rather than as prose
# at five call sites: the validator passes a discriminator, never a sentence, so
# no branch can drift its wording away from the rule it enforces.
_SERVE_ARGUMENT_DEFECTS: dict[str, tuple[str, str]] = {
    "no_arguments": (
        "the call carried no arguments.",
        "re-send `serve` with the required string fields `title` and `path`; set "
        "`kind` to exactly `app` or `files` only if the documented `app` default "
        "is wrong for this handoff.",
    ),
    "title_missing": (
        "`title` was empty or missing.",
        "re-send the same `path` with a non-empty human-readable `title` "
        "describing what you are handing off.",
    ),
    "path_missing": (
        "`path` was empty or missing.",
        "re-send the same `title` with a non-empty workspace-relative entry path.",
    ),
    "kind_invalid": (
        "`kind` was {detail!r}, which is neither `app` nor `files`.",
        "re-send the same entry with `kind` set to exactly `app` or `files`, or "
        "omit `kind` to take the documented `app` default.",
    ),
    "path_unresolvable": (
        "`path` normalized to nothing (you sent {detail!r}, which resolves "
        "outside or above the workspace root).",
        "re-send a workspace-relative entry FILE path, such as `dist/index.html`.",
    ),
}


def serve_argument_refusal(repeats: int, step: AgentStep, defect: str, detail: str = "") -> str:
    """Argument-shape refusal PROJECTED from the call that was refused.

    Five byte-identical canned refusals became one rendering that names what was
    actually received and exactly ONE next move. `defect` is a key into the one
    owner above; the caller never hands prose in.
    """
    problem, fix = _SERVE_ARGUMENT_DEFECTS[defect]
    arguments = (step.tool_call.arguments or {}) if step.tool_call is not None else {}
    sent = (
        ", ".join(f"{name}={arguments.get(name)!r}" for name in ("title", "path", "kind"))
        if arguments
        else "no arguments at all"
    )
    return serve_spend_escalation(
        repeats,
        "for its argument shape",
        f"serve refused: {problem.format(detail=detail)}\n"
        f"You sent: {sent}.\nNext move: {fix}",
    )


def questions_v2_attempts_since_last_plan(events: list[Event]) -> int:
    """Structured intake rounds recorded since the last plan, plus this one.

    The "you already used your one round" refusal fires on every further
    attempt, so it must be able to say WHICH attempt this is.
    """
    from ..events import QuestionsV2Event

    count = 0
    for event in reversed(events):
        if isinstance(event, PlanEvent):
            break
        if isinstance(event, QuestionsV2Event):
            count += 1
    return count + 1


_SERVE_TARGET_SHAPE_KINDS = {
    "app": 'kind="app" (the interactive app entry)',
    "files": 'kind="files" (the artifact-files entry)',
}


def serve_target_shape_guidance(expected_kind: str, offered_kind: str, repeats: int) -> str:
    """Shape refusal that NAMES the admitted kind and ESCALATES.

    Counted wave-1 FAIL 2026-08-06 (`diag_script_run` seed 7, ACTIONLESS_THRASH).
    The agent finished the CLI task correctly, then offered a files-shaped
    handoff against a contract admitting only the app shape. The refusal below
    told it the kind "does not match" and to use "the requested interactive app
    or artifact-files shape" — without ever saying WHICH of the two this target
    requested, though `_expected_serve_kind` had just computed it. Byte-identical
    13 times; retry was the only move left, and retry is what the loop-breaker
    graded.

    This is exactly the sin `host_claims.handoff_clauses` already names and fixes
    one gate downstream: *"an agent told only that its handoff does not match ...
    can do nothing but hand off the identical thing again ... Naming the fact is
    what turns an unrecoverable loop into one corrective move."* The finish gate
    got that doctrine; this serve-time gate never did. Applying it here.

    Deliberately NOT a threshold change and not a relaxation of the contract: the
    admitted shape still governs, a matching handoff is accepted exactly as
    before, and the actionless cap is untouched. What changes is that the agent
    is told the one fact that makes a corrective move possible, and that a
    deliverable which genuinely cannot take the admitted shape reaches a NAMED
    impasse instead of an unbounded identical loop.
    """

    admitted = _SERVE_TARGET_SHAPE_KINDS.get(expected_kind, f"kind={expected_kind!r}")
    offered = _SERVE_TARGET_SHAPE_KINDS.get(offered_kind, f"kind={offered_kind!r}")
    # Constraint 4: the generic sentence used to be a module constant
    # (`_SERVE_TARGET_SHAPE_GUIDANCE`) spliced in ahead of the derived line.
    # It has exactly one consumer, so it is folded in here — the refusal is now
    # ONE rendered string with no canned fragment able to be reused elsewhere.
    named = (
        "serve refused: this handoff kind does not match the current "
        "target-owned delivery contract. Hand off the exact admitted entry; "
        "attachments cannot replace the canonical target handoff.\n"
        f"This target admits {admitted}; you offered {offered}."
    )
    if repeats <= 1:
        return f"{named}\nRe-send the same entry with {admitted}."
    return (
        "<system-reminder>\n"
        f"STOP — `serve` has now been refused for the same shape mismatch "
        f"{repeats} times, with the identical reason each time. {named}\n"
        "`serve` is not counted as work, so each refused attempt spends one turn "
        "toward the no-progress limit that ENDS this run.\n"
        f"There are exactly two moves left: re-send the entry with {admitted}, or "
        "— if this deliverable genuinely cannot take that shape — say so plainly "
        "and call `finish`, which will record the exact unmet contract fact. "
        "Do not repeat the refused shape again.\n"
        "</system-reminder>"
    )

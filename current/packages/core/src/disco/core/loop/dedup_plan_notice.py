"""F47 — the answered-question NOTICE for the PLAN-TRACKING tool class.

F47's ratified general invariant is *"answered-question repetition is culpable
only after notice; broken-tool repetition is culpable immediately."* The
2026-08-06x repair made the loop and the oracle share an IDENTITY owner and
proved it (`test_every_repetition_shape_declares_a_shared_identity_owner`). That
closed a real gap and it is not what failed next.

What failed is REACH. `ThrashOracle` counts SHAPE_IDENTICAL_STREAK over EVERY
tool — `_longest_identical_streak` keys on `tool_call_fingerprint` and excludes
nothing — while the notice could only be emitted for `_W39_SHELL_TOOLS`, because
`_prepare_observation` early-returned for any other tool when assist is off. So
the shape had a declared owner, the enforcement test passed, and the notice was
still unreachable for every non-shell tool.

Measured live at 2026-08-07d: `p4_ff_node_restart`@98651 issued an IDENTICAL
`update_plan_progress` at seqs 229/232/235 against a cap of 2, each returning
`success=True` / `plan progress: 2/2 done` — answered-question repetition by
F47's own definition — and the FIRST feedback body arrived at seq 237, after the
cap was already exceeded. Culpability with no prior notice. Adjudicated IN SCOPE
by the 2026-08-07e review; F47 moved to REPAIRED-PARTIAL.

This is the same defect shape as F51 one level up, and the 2026-08-07e
Enumerated-Class Invariant names it: a green produced by an instrument whose
enumerated class is a proper subset of the class the rule names.

**The cap is NOT widened and the oracle is NOT weakened.** The defect is the
missing notice, not the counting; `max_identical_action_repeats` stays 2 and
`ThrashOracle` is untouched. All this does is make the product say, before the
repeat executes, what it already knew.

Lives in its own module rather than in `dedup.py` because that file is at its
logical-size budget; the tool-class declarations (`_W39_PLAN_TOOLS`,
`_W39_NOTICE_TOOLS`) stay there beside `_W39_SHELL_TOOLS` so every covered class
is declared in one place.
"""

from __future__ import annotations

from ..events import ActionEvent, Event
from ..tool_fingerprint import tool_call_fingerprint
from .dedup import (
    _W39_PLAN_TOOLS,
    _W39_RESULT_BLOCK,
    _f9_has_successful_observation,
    _w39_freshness_boundary_seq,
    _w39_prior_result_excerpt,
    _w39_reminder_emitted_after,
)

# 2026-08-07n: the consequence sentence and the identical-call counter moved to
# `dedup_notice_common` so the shell/script class can share them. ONE owner, not
# a copy — a second copy of a message a test then attests is the F58 defect.
from .dedup_notice_common import _W39_CAP_CONSEQUENCE, _w39_identical_call_count

# Distinct sentinel, for the same reason the script class got one: the anti-spam
# scan is a substring match, and a bracketed suffix cannot collide with
# `[W-39 verify-dedup]`.
_W39_PLAN_REMINDER_SENTINEL = "[W-39 verify-dedup:plan]"

# Repetition-aware by construction (GROUNDED FEEDBACK constraint 4): the count is
# in the template, so this surface cannot emit the same bytes twice in one run
# without the run's own identical-call count having failed to advance.
# 2026-08-07n: the consequence sentence is now the SHARED owner in `dedup`
# (`_W39_CAP_CONSEQUENCE`), because the shell/script class says the same thing
# and two copies of one sentence is the F58 pattern. The RENDERED BYTES of this
# template are unchanged — this is a re-composition, not a message change, and
# the plan class stays exactly as ratified under F47.
_W39_PLAN_REMINDER_TEMPLATE = (
    "<system-reminder>\n"
    "{sentinel} You have already made this exact `{tool}` call {count} times in "
    "this run — the first, at step {step}, SUCCEEDED and this run's record shows "
    "no change since then.{result}\n"
    "Repeating it asks a question you already have the answer to, and it is "
    "counted: " + _W39_CAP_CONSEQUENCE + ". Act on the state above, or do the "
    "next real step.\n"
    "</system-reminder>"
)


def _w39_plan_progress_reminder(
    current_tool: str | None,
    current_args: dict | None,
    events: list[Event],
) -> tuple[bool, int, str]:
    """F47 — ADVISORY notice before an identical plan-tracking call repeats.

    Same posture as the shell memo: it NEVER skips, the call always executes, and
    the only observable change is one advisory MessageEvent before it. Same three
    borrowed mechanisms, deliberately not re-implemented:

    * identity — `tool_call_fingerprint`, the owner the oracle grades with;
    * currency — `_w39_freshness_boundary_seq`, the SHARED predicate
      (`disco.core.receipt_currency`), so the echo and the cap read one rule
      (GROUNDED FEEDBACK constraint 2, "one currency predicate, two consumers");
    * anti-spam — `_w39_reminder_emitted_after`, pivoting on the OCCURRENCE, which
      is the F47 fix that keeps a notice inside the counted group instead of
      spending its one shot upstream of it.

    Unlike the shell class there is no window bound: plan-tracking calls are cheap
    to scan and a bookkeeping loop can run long, so bounding the walk here would
    reintroduce exactly the silence this repairs.

    Returns ``(False, 0, "")`` when no notice should fire.
    """
    if current_tool not in _W39_PLAN_TOOLS or not isinstance(current_args, dict):
        return (False, 0, "")

    prior_events = events[:-1] if events else []
    boundary_seq = _w39_freshness_boundary_seq(prior_events)
    fingerprint = tool_call_fingerprint(current_tool, current_args)

    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if tool_call_fingerprint(e.tool_call.tool_name, e.tool_call.arguments) != fingerprint:
            continue
        # Only a SUCCESSFUL prior run is an answered question. A failed one is
        # broken-tool repetition, which F47's invariant says is culpable
        # immediately and which must not be dressed as "you already have this".
        if not _f9_has_successful_observation(events, e.id):
            continue
        prior_seq = e.seq or 0
        if boundary_seq >= prior_seq:
            return (False, 0, "")
        if _w39_reminder_emitted_after(
            events, _W39_PLAN_REMINDER_SENTINEL, fingerprint, prior_seq
        ):
            return (False, 0, "")
        prior_result = _w39_prior_result_excerpt(events, e.id)
        return (
            True,
            prior_seq,
            _W39_PLAN_REMINDER_TEMPLATE.format(
                sentinel=_W39_PLAN_REMINDER_SENTINEL,
                tool=current_tool,
                count=_w39_identical_call_count(prior_events, fingerprint),
                step=prior_seq,
                result=(
                    _W39_RESULT_BLOCK.format(result=prior_result) if prior_result else ""
                ),
            ),
        )
    return (False, 0, "")



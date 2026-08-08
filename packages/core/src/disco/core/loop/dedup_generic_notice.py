"""F59 — generic answered-question notice for EVERY tool the oracle counts.

ThrashOracle counts byte-identical streaks over EVERY tool via
`tool_call_fingerprint` with no filter. The notice previously reached only
`{shell, shell_exec, plan_step, update_plan_progress}` (dedup.py:980), so
`verify_web_app` ×3 at 07h@99603 (seqs 343/346/349, all success=True) was
condemned with no notice reachable — F47's defect one class over, third
occurrence.

This module closes the gap: a generic notice that fires for any tool not
already covered by the shell/script or plan classes. It reuses the same three
shared mechanisms, never reimplemented:

* identity — `tool_call_fingerprint`, the owner the oracle grades with;
* currency — `_w39_freshness_boundary_seq`, the shared predicate
  (`disco.core.receipt_currency`), so echo and cap read one rule;
* anti-spam — `_w39_reminder_emitted_after`, pivoting on the OCCURRENCE.

It is repetition-aware by construction (the count is in the template) and names
the consequence via the single owner `_W39_CAP_CONSEQUENCE`. Shell and plan
retain their richer renderers; this generic path is reachable for every other
class without duplicating their prose — a second copy of a message a test
attests would be the F58 defect.

Exclusions: `propose_plan_update` and `submit_plan` are deliberately excluded
here as they are in the plan class — `_auto_approve_revision` already emits
its own identical-revision nudge. That exclusion is owned by the oracle's
counted population too: the oracle counts them, but `check_streak_thrash`
excludes them via `approved_plan_predicate_scope` (a plan-predicate scope
change breaks the streak), so the exclusion is proved non-weakening. A generic
notice that fired there would double-fire. The test
`test_the_notice_reach_residue_is_declared_rather_than_implied` is updated
to reflect the new covered set; no parallel silent list remains.
"""

from __future__ import annotations

from ..events import ActionEvent, Event
from ..tool_fingerprint import tool_call_fingerprint
from .dedup import (
    _W39_RESULT_BLOCK,
    _f9_has_successful_observation,
    _w39_freshness_boundary_seq,
    _w39_prior_result_excerpt,
    _w39_reminder_emitted_after,
)
from .dedup_notice_common import _W39_CAP_CONSEQUENCE, _w39_identical_call_count

_W39_GENERIC_REMINDER_SENTINEL = "[W-39 verify-dedup:generic]"

# Repetition-aware, generic, and consequence-naming — the minimal shape the
# constraint asks for. It names the tool, the count, the prior step, and the
# cap consequence, and it carries the prior result when available.
_W39_GENERIC_REMINDER_TEMPLATE = (
    "<system-reminder>\n"
    "{sentinel} You have already made this exact `{tool}` call {count} times in "
    "this run — the first, at step {step}, SUCCEEDED and this run's record shows "
    "no change since then.{result}\n"
    "Repeating it asks a question you already have the answer to, and it is "
    "counted: " + _W39_CAP_CONSEQUENCE + ". Act on the state above, or do the "
    "next real step.\n"
    "</system-reminder>"
)

# Per binding correction #1: prefer no silent exclusions. Shell and plan
# classes retain their richer renderers; every other tool reaches the generic
# notice. The previous two-tool exclusion is removed — coverage is now
# total and vacuous for the exclusion proof. If a future exclusion is
# genuinely needed, it must be proved by a richer notice or by the oracle's
# own counted predicate (not by asserting the set alone).
_W39_GENERIC_EXCLUDED: frozenset[str] = frozenset()


def _generic_prior_event(
    fingerprint: str,
    prior_events: list[Event],
    events: list[Event],
    boundary_seq: int,
) -> ActionEvent | None:
    """Find the success-backed prior that still carries currency."""
    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if tool_call_fingerprint(e.tool_call.tool_name, e.tool_call.arguments) != fingerprint:
            continue
        if not _f9_has_successful_observation(events, e.id):
            continue
        prior_seq = e.seq or 0
        if boundary_seq >= prior_seq:
            return None
        if _w39_reminder_emitted_after(
            events, _W39_GENERIC_REMINDER_SENTINEL, fingerprint, prior_seq
        ):
            return None
        return e
    return None


def _w39_generic_reminder(
    current_tool: str | None,
    current_args: dict | None,
    events: list[Event],
) -> tuple[bool, int, str]:
    """Generic answered-question notice for any tool not covered by shell/plan.

    Same posture as the other classes: NEVER skips, the call always executes,
    the only observable change is one advisory MessageEvent before it.

    Returns ``(False, 0, "")`` when no notice should fire.
    """
    if not current_tool or not isinstance(current_args, dict):
        return (False, 0, "")
    if current_tool in _W39_GENERIC_EXCLUDED:
        return (False, 0, "")
    from .dedup import _W39_PLAN_TOOLS, _W39_SHELL_TOOLS

    if current_tool in _W39_SHELL_TOOLS or current_tool in _W39_PLAN_TOOLS:
        return (False, 0, "")
    prior_events = events[:-1] if events else []
    boundary_seq = _w39_freshness_boundary_seq(prior_events)
    fingerprint = tool_call_fingerprint(current_tool, current_args)
    prior = _generic_prior_event(fingerprint, prior_events, events, boundary_seq)
    if prior is None:
        return (False, 0, "")
    prior_seq = prior.seq or 0
    prior_result = _w39_prior_result_excerpt(events, prior.id)
    return (
        True,
        prior_seq,
        _W39_GENERIC_REMINDER_TEMPLATE.format(
            sentinel=_W39_GENERIC_REMINDER_SENTINEL,
            tool=current_tool,
            count=_w39_identical_call_count(prior_events, fingerprint),
            step=prior_seq,
            result=_W39_RESULT_BLOCK.format(result=prior_result) if prior_result else "",
        ),
    )

"""Order 0 — ModelExecutionPolicy + resolve_policy (assist-pipeline refactor).

The policy is the single source of truth for model-tier execution. These lock in the
two properties the whole refactor relies on: (1) `standard` runs the pre-assist code
path with NO weak-model compensations — its ONLY tool-surface change is the universal
`plan_step` retirement (see below), and (2) the tier + anchored-edit capability live on
ONE object so the tool surface and the compensations can't disagree. Plus the resolve
precedence is behavior-preserving.

`plan_step` retirement (runthru-v2 #3): incremental per-step `plan_step` updates caused
plan-state drift and failed across ALL models, so `plan_step` is withheld from the
advertised surface for EVERY tier (the declarative `update_plan_progress` replaced it).
Standard therefore withholds exactly {plan_step}; it is no longer a pure no-op.
"""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy, resolve_policy


def test_standard_retires_only_plan_step():
    """Standard runs the no-op (no-compensation) path: assist is off and the ONLY
    advertised-surface change is the universal `plan_step` retirement (runthru-v2 #3 —
    incremental plan_step caused state-drift across ALL tiers). It withholds nothing
    else; `update_plan_progress` remains advertised for standard."""
    p = ModelExecutionPolicy.standard()
    assert p.tier == "standard"
    assert p.assist is False
    # plan_step is retired for EVERY tier; standard withholds exactly that and nothing more
    assert p.withheld_tools == frozenset({"plan_step"})


def test_weak_withholds_progress_tools():
    p = ModelExecutionPolicy(tier="weak", anchored_edit=True)
    assert p.assist is True
    # weak models get the honest NL plan — no per-step bookkeeping tools
    assert p.withheld_tools == frozenset({"plan_step", "update_plan_progress"})


def test_anchored_edit_capability_is_independent_of_tier():
    # a capable (standard) model that still lacks anchored edit loses file_str_replace —
    # plus plan_step, which is retired for ALL tiers (runthru-v2 #3 state-drift)
    p = ModelExecutionPolicy(tier="standard", anchored_edit=False)
    assert p.assist is False
    assert p.withheld_tools == frozenset({"plan_step", "file_str_replace"})
    # weak + non-anchored withholds all three
    q = ModelExecutionPolicy(tier="weak", anchored_edit=False)
    assert q.withheld_tools == frozenset({"plan_step", "update_plan_progress", "file_str_replace"})


def test_resolve_precedence_override_wins():
    # explicit per-conversation toggle beats everything
    assert resolve_policy(
        assist_override=False, entry_tier="weak", hosting_weak_default=True, anchored_edit=True
    ).tier == "standard"
    assert resolve_policy(
        assist_override=True, entry_tier="standard", hosting_weak_default=False, anchored_edit=True
    ).tier == "weak"


def test_resolve_precedence_entry_tier_then_hosting():
    # no override → explicit ModelEntry.tier wins over the hosting heuristic
    assert resolve_policy(
        assist_override=None, entry_tier="standard", hosting_weak_default=True, anchored_edit=True
    ).tier == "standard"
    # no override, no entry tier → fall back to the hosting heuristic (back-compat)
    assert resolve_policy(
        assist_override=None, entry_tier=None, hosting_weak_default=True, anchored_edit=False
    ).tier == "weak"
    assert resolve_policy(
        assist_override=None, entry_tier=None, hosting_weak_default=False, anchored_edit=True
    ).tier == "standard"


def test_anchored_edit_threaded_through_resolve():
    assert resolve_policy(
        assist_override=None, entry_tier="weak", hosting_weak_default=True, anchored_edit=False
    ).anchored_edit is False

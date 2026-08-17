"""Order 0 — ModelExecutionPolicy + resolve_policy (assist-pipeline refactor).

The policy is the single source of truth for model-tier execution. These lock in the
two properties the whole refactor relies on: (1) `standard` runs the pre-assist code
path with NO weak-model compensations, and (2) the tier + anchored-edit capability live
on ONE object so the tool surface and the compensations can't disagree. Plus the resolve
precedence is behavior-preserving. Retired tools that are no longer in AGENT_TOOLS stay
out of `withheld_tools`; the policy only withholds tools still present in the surface.
"""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy, resolve_policy


def test_standard_withholds_nothing():
    """Standard anchored runs the no-op path and advertises the full remaining scope."""
    p = ModelExecutionPolicy.standard()
    assert p.tier == "standard"
    assert p.assist is False
    assert p.withheld_tools == frozenset()


def test_weak_withholds_progress_tools():
    p = ModelExecutionPolicy(tier="weak", anchored_edit=True)
    assert p.assist is True
    # weak models get the honest NL plan — no declarative progress snapshot burden
    assert p.withheld_tools == frozenset({"update_plan_progress"})


def test_anchored_edit_capability_is_independent_of_tier():
    # a capable (standard) model that still lacks anchored edit loses exact_replace
    p = ModelExecutionPolicy(tier="standard", anchored_edit=False)
    assert p.assist is False
    assert p.withheld_tools == frozenset({"exact_replace"})
    # weak + non-anchored withholds those plus update_plan_progress
    q = ModelExecutionPolicy(tier="weak", anchored_edit=False)
    assert q.withheld_tools == frozenset({"update_plan_progress", "exact_replace"})


def test_resolve_precedence_override_wins():
    # explicit per-conversation toggle beats everything
    assert (
        resolve_policy(
            assist_override=False, entry_tier="weak", hosting_weak_default=True, anchored_edit=True
        ).tier
        == "standard"
    )
    assert (
        resolve_policy(
            assist_override=True,
            entry_tier="standard",
            hosting_weak_default=False,
            anchored_edit=True,
        ).tier
        == "weak"
    )


def test_resolve_precedence_entry_tier_then_hosting():
    # no override → explicit ModelEntry.tier wins over the hosting heuristic
    assert (
        resolve_policy(
            assist_override=None,
            entry_tier="standard",
            hosting_weak_default=True,
            anchored_edit=True,
        ).tier
        == "standard"
    )
    # no override, no entry tier → fall back to the hosting heuristic (back-compat)
    assert (
        resolve_policy(
            assist_override=None, entry_tier=None, hosting_weak_default=True, anchored_edit=False
        ).tier
        == "weak"
    )
    assert (
        resolve_policy(
            assist_override=None, entry_tier=None, hosting_weak_default=False, anchored_edit=True
        ).tier
        == "standard"
    )


def test_anchored_edit_threaded_through_resolve():
    assert (
        resolve_policy(
            assist_override=None, entry_tier="weak", hosting_weak_default=True, anchored_edit=False
        ).anchored_edit
        is False
    )

"""Order 0 — ModelExecutionPolicy + resolve_policy (assist-pipeline refactor).

The policy is the single source of truth for model-tier execution. These lock in the
two properties the whole refactor relies on: (1) `standard` is a provable no-op, and
(2) the tier + anchored-edit capability live on ONE object so the tool surface and the
compensations can't disagree. Plus the resolve precedence is behavior-preserving.
"""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy, resolve_policy


def test_standard_is_a_noop():
    p = ModelExecutionPolicy.standard()
    assert p.tier == "standard"
    assert p.assist is False
    assert p.withheld_tools == frozenset()  # advertises everything


def test_weak_withholds_progress_tools():
    p = ModelExecutionPolicy(tier="weak", anchored_edit=True)
    assert p.assist is True
    # weak models get the honest NL plan — no per-step bookkeeping tools
    assert p.withheld_tools == frozenset({"plan_step", "update_plan_progress"})


def test_anchored_edit_capability_is_independent_of_tier():
    # a capable (standard) model that still lacks anchored edit only loses file_str_replace
    p = ModelExecutionPolicy(tier="standard", anchored_edit=False)
    assert p.assist is False
    assert p.withheld_tools == frozenset({"file_str_replace"})
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

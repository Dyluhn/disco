"""Order B acceptance tests — policy-driven tool-surface + prompt contamination.

Contract: the SAME ModelExecutionPolicy object that drives the assist compensations
also drives the advertised tool surface (executor.available_tools) and the prompt
text (DriverPrompts.system_prompt).  These tests are the acceptance gate:

  1. weak policy  → available_tools() EXCLUDES plan_step + update_plan_progress
  2. standard     → available_tools() EXCLUDES plan_step (RETIRED for ALL tiers per
                    runthru-v2 #3 — state-drift; declarative update_plan_progress replaced
                    it) but still INCLUDES update_plan_progress
  3. standard + anchored_edit=False → withholds file_str_replace (plan_step is already
                    withheld for every tier)
  4. prompt-contamination: NO prompt (weak OR standard, planning OR execution) names
     `plan_step` — it is RETIRED from the advertised surface for every tier, so naming
     it anywhere is a false affordance (unknown-tool error). Positive gate: the standard
     execution prompt still names `update_plan_progress` (the declarative replacement);
     the weak prompts name neither progress tool.
"""

from __future__ import annotations

import pytest
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.llm.prompts import DriverPrompts
from disco.core.llm.types import ModelRole
from disco.tools.builtin import build_default_registry
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import agent_scope

# Policies used across tests
_STANDARD = ModelExecutionPolicy.standard()  # tier="standard", anchored_edit=True
_WEAK = ModelExecutionPolicy(tier="weak", anchored_edit=True)
_STANDARD_NO_ANCHORED = ModelExecutionPolicy(tier="standard", anchored_edit=False)
_WEAK_NO_ANCHORED = ModelExecutionPolicy(tier="weak", anchored_edit=False)


def _make_executor(policy: ModelExecutionPolicy) -> DefaultToolExecutor:
    """Build an executor with the full default registry + given policy."""
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=policy),
        model_policy=policy,
    )


def _tool_names(policy: ModelExecutionPolicy) -> set[str]:
    return {t.name for t in _make_executor(policy).available_tools()}


# ---------------------------------------------------------------------------
# 1. weak policy — plan_step and update_plan_progress excluded
# ---------------------------------------------------------------------------


def test_weak_policy_excludes_plan_step():
    """Weak tier withholds plan_step from available_tools()."""
    names = _tool_names(_WEAK)
    assert "plan_step" not in names, (
        "weak policy must NOT advertise plan_step (it is withheld for the weak tier)"
    )


def test_weak_policy_excludes_update_plan_progress():
    """Weak tier withholds update_plan_progress from available_tools()."""
    names = _tool_names(_WEAK)
    assert "update_plan_progress" not in names, (
        "weak policy must NOT advertise update_plan_progress"
    )


def test_weak_policy_still_advertises_file_str_replace_when_anchored():
    """Weak tier with anchored_edit=True: file_str_replace is NOT withheld
    (only the progress tools are; anchored-edit capability is independent)."""
    names = _tool_names(_WEAK)
    assert "file_str_replace" in names, (
        "weak + anchored_edit=True should still advertise file_str_replace"
    )


# ---------------------------------------------------------------------------
# 2. standard — update_plan_progress advertised; plan_step RETIRED (all tiers)
# ---------------------------------------------------------------------------


def test_standard_policy_advertises_plan_step():
    """Standard tier must NOT offer plan_step — it is RETIRED from the advertised
    surface for EVERY tier (runthru-v2 #3: incremental plan_step caused plan-state
    drift across all models; the declarative update_plan_progress replaced it). It
    stays callable for defensive back-compat but is never advertised."""
    names = _tool_names(_STANDARD)
    assert "plan_step" not in names


def test_standard_policy_advertises_update_plan_progress():
    """Standard tier must offer update_plan_progress in available_tools()."""
    names = _tool_names(_STANDARD)
    assert "update_plan_progress" in names


def test_standard_policy_advertises_file_str_replace():
    """Standard tier with anchored_edit=True advertises file_str_replace."""
    names = _tool_names(_STANDARD)
    assert "file_str_replace" in names


# ---------------------------------------------------------------------------
# 3. standard + anchored_edit=False → withholds ONLY file_str_replace
# ---------------------------------------------------------------------------


def test_non_anchored_standard_withholds_file_str_replace():
    """standard + anchored_edit=False: file_str_replace is NOT in available_tools()."""
    names = _tool_names(_STANDARD_NO_ANCHORED)
    assert "file_str_replace" not in names


def test_non_anchored_standard_keeps_plan_step():
    """standard + anchored_edit=False: plan_step is withheld — it is RETIRED from the
    advertised surface for EVERY tier (runthru-v2 #3, state-drift), independent of the
    anchored-edit capability. This variant additionally withholds file_str_replace."""
    names = _tool_names(_STANDARD_NO_ANCHORED)
    assert "plan_step" not in names


def test_non_anchored_standard_keeps_update_plan_progress():
    """standard + anchored_edit=False: update_plan_progress is NOT withheld."""
    names = _tool_names(_STANDARD_NO_ANCHORED)
    assert "update_plan_progress" in names


def test_non_anchored_standard_withholds_exactly_file_str_replace():
    """standard + anchored_edit=False withholds EXACTLY the anchored-edit tools
    {file_str_replace, exact_replace} — no extras (CD-TOOLS-2 added exact_replace to that tier)."""
    standard_names = _tool_names(_STANDARD)
    non_anchored_names = _tool_names(_STANDARD_NO_ANCHORED)
    dropped = standard_names - non_anchored_names
    assert dropped == {"file_str_replace", "exact_replace"}, (
        f"non-anchored standard should drop only the anchored-edit tools, got: {dropped}"
    )


# ---------------------------------------------------------------------------
# 4. prompt-contamination: weak prompts must not name withheld tools
# ---------------------------------------------------------------------------


def _driver() -> DriverPrompts:
    return DriverPrompts()


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.INTERACTIVE])
def test_weak_prompt_does_not_name_plan_step(mode: OperatingMode):
    """Weak-tier prompt for PLANNING and EXECUTION must not mention 'plan_step'.

    plan_step is withheld from the tool list for weak models; if it appears in the
    prompt the model will attempt to call it and get an unknown-tool error."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    # The literal tool name as it would appear in a prompt (backtick-quoted or bare).
    assert "`plan_step`" not in prompt, (
        f"weak-tier {mode.value} prompt must not mention `plan_step` "
        "(it is withheld from the tool surface)"
    )
    assert "plan_step" not in prompt, (
        f"weak-tier {mode.value} prompt must not mention plan_step at all"
    )


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.INTERACTIVE])
def test_weak_prompt_does_not_name_update_plan_progress(mode: OperatingMode):
    """Weak-tier prompt must not mention 'update_plan_progress'."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "update_plan_progress" not in prompt, (
        f"weak-tier {mode.value} prompt must not mention update_plan_progress"
    )


def test_standard_execution_prompt_names_update_plan_progress():
    """Standard (capable) execution prompt SHOULD mention update_plan_progress — positive gate."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.INTERACTIVE,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "update_plan_progress" in prompt, (
        "standard execution prompt must mention update_plan_progress (it is NOT withheld)"
    )


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.INTERACTIVE])
def test_standard_prompt_does_not_name_plan_step(mode: OperatingMode):
    """Standard (capable, assist=False) prompt for PLANNING and EXECUTION must NOT
    mention `plan_step` — it is RETIRED from the advertised tool surface for EVERY tier
    (runthru-v2 #3, plan-state drift). Naming it is a false affordance: the model would
    try to call a tool that is not in its tool list and get an unknown-tool error."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "`plan_step`" not in prompt, (
        f"standard {mode.value} prompt must not mention `plan_step` (retired for all tiers)"
    )
    assert "plan_step" not in prompt, (
        f"standard {mode.value} prompt must not mention plan_step at all (retired for all tiers)"
    )


def test_standard_planning_prompt_keeps_other_meta_tools():
    """Removing plan_step must not strip the rest of the meta-tool capability line —
    the standard planning prompt still advertises preview_start / serve / server_status."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    for tool in ("preview_start", "serve", "server_status"):
        assert f"`{tool}`" in prompt, (
            f"standard planning prompt should still advertise {tool}"
        )


def test_non_anchored_prompt_does_not_mention_file_str_replace():
    """No prompt variant (weak or standard) should mention file_str_replace —
    it is not currently named in any prompt template, so this is a no-regression guard."""
    dp = _driver()
    for assist in (True, False):
        for mode in (OperatingMode.PLANNING, OperatingMode.INTERACTIVE):
            prompt = dp.system_prompt(
                model_family="qwen",
                mode=mode,
                role=ModelRole.AGENT_DRIVER,
                assist=assist,
            )
            assert "file_str_replace" not in prompt, (
                f"assist={assist} {mode.value} prompt must not mention file_str_replace "
                "(use file_edit / file_replace_lines instead)"
            )

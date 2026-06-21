"""Order B acceptance tests — policy-driven tool-surface + prompt contamination.

Contract: the SAME ModelExecutionPolicy object that drives the assist compensations
also drives the advertised tool surface (executor.available_tools) and the prompt
text (DriverPrompts.system_prompt).  These tests are the acceptance gate:

  1. weak policy  → available_tools() EXCLUDES plan_step + update_plan_progress
  2. standard     → available_tools() INCLUDES plan_step + update_plan_progress
  3. standard + anchored_edit=False → withholds ONLY file_str_replace
  4. prompt-contamination: weak-policy prompts (execution + planning) name NONE
     of the withheld tools; standard prompts name them (positive gate).
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
# 2. standard — plan_step and update_plan_progress advertised
# ---------------------------------------------------------------------------


def test_standard_policy_advertises_plan_step():
    """Standard tier must offer plan_step in available_tools()."""
    names = _tool_names(_STANDARD)
    assert "plan_step" in names


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
    """standard + anchored_edit=False: plan_step is NOT withheld (only file_str_replace is)."""
    names = _tool_names(_STANDARD_NO_ANCHORED)
    assert "plan_step" in names


def test_non_anchored_standard_keeps_update_plan_progress():
    """standard + anchored_edit=False: update_plan_progress is NOT withheld."""
    names = _tool_names(_STANDARD_NO_ANCHORED)
    assert "update_plan_progress" in names


def test_non_anchored_standard_withholds_exactly_file_str_replace():
    """standard + anchored_edit=False withholds EXACTLY {file_str_replace} — no extras."""
    standard_names = _tool_names(_STANDARD)
    non_anchored_names = _tool_names(_STANDARD_NO_ANCHORED)
    dropped = standard_names - non_anchored_names
    assert dropped == {"file_str_replace"}, (
        f"non-anchored standard should drop only file_str_replace, got: {dropped}"
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


def test_standard_planning_prompt_names_plan_step():
    """Standard (capable) planning prompt SHOULD mention plan_step in the capability block."""
    dp = _driver()
    prompt = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "plan_step" in prompt, (
        "standard planning prompt must mention plan_step (it is NOT withheld for capable tier)"
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

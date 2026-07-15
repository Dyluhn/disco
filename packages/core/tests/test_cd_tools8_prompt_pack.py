"""CD-TOOLS-8 — the build/driver prompts teach Claude Design tool discipline. The CAPABLE prompt
names the new tools (exact_replace gated on the anchored-edit capability), the WEAK prompt never
names a withheld anchored-edit tool, and the universal edit discipline (run_project_script /
guarded file_write / fresh-read / no-elision) is in both."""

from __future__ import annotations

import pytest
from disco.core.llm import DriverPrompts, ModelRole, OperatingMode, Requirement


def _prompt(*, assist: bool, capabilities=None, mode=OperatingMode.LONG_HORIZON) -> str:
    return DriverPrompts(flavor="build").system_prompt(
        model_family="qwen",
        mode=mode,
        role=ModelRole.AGENT_DRIVER,
        capabilities=capabilities,
        assist=assist,
    )


def test_capable_anchored_prompt_names_exact_replace():
    p = _prompt(assist=False, capabilities=frozenset({Requirement.ANCHORED_EDIT}))
    assert "exact_replace" in p
    assert "file_read" in p  # fresh-read-before-edit discipline


def test_capable_without_anchored_capability_does_not_name_exact_replace():
    # exact_replace is withheld for a standard-but-non-anchored model → must NOT be named (no false
    # affordance). The gate is the ANCHORED_EDIT capability, not the tier.
    p = _prompt(assist=False, capabilities=frozenset())
    assert "exact_replace" not in p
    assert "file_str_replace" not in p


@pytest.mark.parametrize("mode", [OperatingMode.PLANNING, OperatingMode.LONG_HORIZON])
@pytest.mark.parametrize("assist", [True, False])
def test_non_anchored_prompt_never_names_withheld_anchored_edit_tools(mode, assist):
    # exact_replace + file_str_replace are withheld for a NON-anchored model (any tier). The prompt
    # must NOT name them without the ANCHORED_EDIT capability — the CD-TOOLS-8 contamination guard.
    p = _prompt(assist=assist, mode=mode, capabilities=frozenset())  # no ANCHORED_EDIT
    assert "exact_replace" not in p, (
        f"non-anchored {mode}/assist={assist} must not name exact_replace"
    )
    assert "file_str_replace" not in p, (
        f"non-anchored {mode}/assist={assist} must not name file_str_replace"
    )


def test_capable_execution_prompt_has_universal_discipline():
    p = _prompt(assist=False, capabilities=frozenset())
    assert "run_project_script" in p
    assert "allow_shrink=true" in p
    assert "FRESH_READ_REQUIRED" in p  # the recovery rule
    assert "elided" in p  # the never-echo-elision-marker rule
    assert (
        "whole file for a small" in p.lower() or "whole file for a small" in p
    )  # no-whole-rewrite


def test_weak_execution_prompt_has_universal_discipline_minus_anchored():
    p = _prompt(assist=True, capabilities=frozenset())
    assert "run_project_script" in p
    assert "allow_shrink=true" in p
    assert "elided" in p
    assert "exact_replace" not in p  # still no withheld tool

"""G1 — Execution prompt steers agent to use artifact generator tools.

Root cause from dylans-walkthru-6-17-26.md §G1: the agent hand-rolled HTML for
slides because `_EXECUTION_DRIVER_PROMPT` only mentioned file tools and never
named `slides_generate` / `sheet_generate`. Both execution prompt variants
(capable + small-model) must now contain an explicit directive so that at
execution time the model knows to prefer the generator over file_write.

These tests pin that the directive is present in BOTH variants and that the
assist gate (on/off) does not accidentally suppress it.
"""

from __future__ import annotations

from disco.core.llm import DriverPrompts, ModelRole, OperatingMode
from disco.core.llm.prompts import (
    _EXECUTION_DRIVER_PROMPT,
    _EXECUTION_DRIVER_PROMPT_SMALL,
)


# ---------------------------------------------------------------------------
# 1. The constants themselves contain the directives (source of truth check).
# ---------------------------------------------------------------------------


def test_execution_prompt_constant_mentions_slides_generate():
    """The capable-model constant must name slides_generate."""
    assert "slides_generate" in _EXECUTION_DRIVER_PROMPT, (
        "_EXECUTION_DRIVER_PROMPT must mention slides_generate"
    )


def test_execution_prompt_constant_mentions_sheet_generate():
    """The capable-model constant must name sheet_generate."""
    assert "sheet_generate" in _EXECUTION_DRIVER_PROMPT, (
        "_EXECUTION_DRIVER_PROMPT must mention sheet_generate"
    )


def test_execution_prompt_small_constant_mentions_slides_generate():
    """The small-model variant must also name slides_generate."""
    assert "slides_generate" in _EXECUTION_DRIVER_PROMPT_SMALL, (
        "_EXECUTION_DRIVER_PROMPT_SMALL must mention slides_generate"
    )


def test_execution_prompt_small_constant_mentions_sheet_generate():
    """The small-model variant must also name sheet_generate."""
    assert "sheet_generate" in _EXECUTION_DRIVER_PROMPT_SMALL, (
        "_EXECUTION_DRIVER_PROMPT_SMALL must mention sheet_generate"
    )


# ---------------------------------------------------------------------------
# 2. The directive forbids hand-authoring (don't just mention — must warn).
# ---------------------------------------------------------------------------


def test_execution_prompt_discourages_hand_authored_html_for_slides():
    """The capable prompt must instruct the model not to hand-author HTML decks."""
    assert "HTML" in _EXECUTION_DRIVER_PROMPT and "slides_generate" in _EXECUTION_DRIVER_PROMPT, (
        "_EXECUTION_DRIVER_PROMPT must contrast slides_generate against HTML"
    )


def test_execution_prompt_small_discourages_hand_authored_output():
    """The small-model variant must instruct the model not to use file_write for slides/sheets."""
    assert "file_write" in _EXECUTION_DRIVER_PROMPT_SMALL, (
        "_EXECUTION_DRIVER_PROMPT_SMALL must mention file_write in the artifact-tools context"
    )


# ---------------------------------------------------------------------------
# 3. The directive reaches the wire under assist=False (capable path).
# ---------------------------------------------------------------------------


def test_capable_execution_prompt_has_slides_directive():
    """Via DriverPrompts, the assist=False path contains the slides/sheet directive."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "slides_generate" in got, "capable execution prompt must mention slides_generate"
    assert "sheet_generate" in got, "capable execution prompt must mention sheet_generate"


def test_capable_execution_prompt_has_slides_directive_agent_flavor():
    """Agent-flavor (task-framed identity) still carries the directive."""
    dp = DriverPrompts(flavor="agent")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert "slides_generate" in got, "agent-flavor execution prompt must mention slides_generate"
    assert "sheet_generate" in got, "agent-flavor execution prompt must mention sheet_generate"


# ---------------------------------------------------------------------------
# 4. The directive reaches the wire under assist=True (small-model path).
# ---------------------------------------------------------------------------


def test_small_model_execution_prompt_has_slides_directive():
    """Via DriverPrompts, the assist=True (small-model) path also contains the directive."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=True,
    )
    assert "slides_generate" in got, "small-model execution prompt must mention slides_generate"
    assert "sheet_generate" in got, "small-model execution prompt must mention sheet_generate"


# ---------------------------------------------------------------------------
# 5. The directive does NOT appear in the planning prompt (execution-only).
#    The planning capability block (E5) already lists slides_generate there;
#    the NEW execution directive is a separate, additional signal at run time.
#    This test just confirms the execution section is not accidentally merged
#    into the planning path.
# ---------------------------------------------------------------------------


def test_artifact_tools_section_present_in_execution_not_planning():
    """The <artifact_tools> section is in the execution prompt, not planning."""
    dp = DriverPrompts()
    exec_got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    plan_got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "artifact_tools" in exec_got, "artifact_tools tag must be in execution prompt"
    assert "artifact_tools" not in plan_got, (
        "artifact_tools tag must NOT appear in planning prompt (it's execution-only)"
    )

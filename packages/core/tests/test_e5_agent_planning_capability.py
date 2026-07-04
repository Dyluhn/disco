"""E5 — Agent-surface planning capability-awareness.

When planning, the engine only exposes read-only tools (search, extract,
file_read, file_list, questions_v2, clarify, submit_plan, think). Without an explicit note
the model may falsely deny owning a browser, shell, slides generator, etc.

This guard verifies:
  1. The agent-flavor planning prompt contains the capability-awareness block
     (specifically the sentinel phrases 'browser' and 'locked until approval').
  2. [R6] The build-flavor planning prompt ALSO contains the block — the build
     planner hides write tools (read_only=False) the same way, so a build-flavor
     plan (e.g. the DR→slides handoff) otherwise insists it "can't use
     slides_generate". Previously the block was agent-only; R6 applies it to both.
  3. The build-flavor output is _PLANNING_DRIVER_PROMPT + the capability block.
"""

from __future__ import annotations

from disco.core.llm import DriverPrompts, ModelRole, OperatingMode
from disco.core.llm.prompts import (
    _AGENT_PLANNING_CAPABILITY_BLOCK,
    _MENTIONED_ELEMENT_GUIDANCE,
    _PLANNING_DRIVER_PROMPT,
)

# ---------------------------------------------------------------------------
# 1. Agent planning prompt contains capability-awareness sentinels
# ---------------------------------------------------------------------------


def test_agent_planning_prompt_contains_browser():
    """Agent-flavor planning prompt references 'browser' so the model knows it
    will gain a browser tool after plan approval."""
    dp = DriverPrompts(flavor="agent")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "browser" in got, "agent planning prompt must mention 'browser'"


def test_agent_planning_prompt_contains_locked_until_approval():
    """Agent-flavor planning prompt uses the phrase 'locked until approval' so
    the model understands the execution tools are gated, not absent."""
    dp = DriverPrompts(flavor="agent")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" in got, (
        "agent planning prompt must include 'locked until approval'"
    )


def test_agent_planning_prompt_contains_full_capability_block():
    """Agent-flavor planning prompt contains the entire _AGENT_PLANNING_CAPABILITY_BLOCK."""
    dp = DriverPrompts(flavor="agent")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert _AGENT_PLANNING_CAPABILITY_BLOCK in got, (
        "agent planning prompt must embed _AGENT_PLANNING_CAPABILITY_BLOCK verbatim"
    )


def test_agent_planning_prompt_mentions_execution_tools():
    """Agent-flavor planning prompt mentions a representative set of execution tools."""
    dp = DriverPrompts(flavor="agent")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    for tool in ("shell", "file_write", "code_exec", "slides_generate", "sheet_generate",
                 "image_generate", "audio_overview"):
        assert tool in got, f"agent planning prompt must mention '{tool}'"


# ---------------------------------------------------------------------------
# 2. [R6] Build planning prompt ALSO contains the capability block
# ---------------------------------------------------------------------------


def test_build_planning_prompt_contains_capability_block():
    """[R6] Build-flavor planning prompt MUST contain 'locked until approval' —
    the build planner hides write tools too, so it needs the same hint or it
    refuses tools like slides_generate during planning."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" in got, (
        "[R6] capability block must appear in the build-flavor planning prompt"
    )
    assert "slides_generate" in got, "[R6] build planner must know about slides_generate"


def test_default_planning_prompt_contains_capability_block():
    """[R6] Default (no flavor arg) planning prompt MUST contain the capability block."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" in got, (
        "[R6] capability block must appear in the default (build) planning prompt"
    )


# ---------------------------------------------------------------------------
# 3. [R6] Build-flavor planning = constant + capability block
# ---------------------------------------------------------------------------


def test_build_planning_prompt_is_constant_plus_block():
    """[R6] Build-flavor planning output is _PLANNING_DRIVER_PROMPT followed by the
    capability block (the block now applies to all flavors; only the agent identity
    swap 'build agent'→'task agent' distinguishes agent from build planning)."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert got == (
        _PLANNING_DRIVER_PROMPT
        + _MENTIONED_ELEMENT_GUIDANCE
        + _AGENT_PLANNING_CAPABILITY_BLOCK
    ), (
        "[R6] build planning must be the base constant + mention guidance + capability block"
    )


# ---------------------------------------------------------------------------
# 4. Capability block appears in planning only, not execution
# ---------------------------------------------------------------------------


def test_agent_capability_block_not_in_execution_prompt():
    """The capability block is planning-phase only — the execution prompt already
    has the real tools in scope, so adding it there would be redundant/confusing."""
    dp = DriverPrompts(flavor="agent")
    exec_got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" not in exec_got, (
        "capability block must not appear in the agent execution prompt"
    )

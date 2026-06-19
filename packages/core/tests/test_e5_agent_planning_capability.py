"""E5 — Agent-surface planning capability-awareness.

When planning, the engine only exposes read-only tools (search, extract,
file_read, file_list, clarify, submit_plan, think).  Without an explicit note
the model may falsely deny owning a browser, shell, slides generator, etc.

This guard verifies:
  1. The agent-flavor planning prompt contains the capability-awareness block
     (specifically the sentinel phrases 'browser' and 'locked until approval').
  2. The build-flavor planning prompt does NOT contain those phrases — the block
     is agent-only and must not bleed into the Build surface.
  3. The build-flavor output is still byte-identical to _PLANNING_DRIVER_PROMPT.
"""

from __future__ import annotations

from disco.core.llm import DriverPrompts, ModelRole, OperatingMode
from disco.core.llm.prompts import (
    _AGENT_PLANNING_CAPABILITY_BLOCK,
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
# 2. Build planning prompt does NOT contain capability-awareness text
# ---------------------------------------------------------------------------


def test_build_planning_prompt_does_not_contain_capability_block():
    """Build-flavor planning prompt must NOT contain 'locked until approval' —
    the capability block is agent-only."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" not in got, (
        "capability block must not appear in the build-flavor planning prompt"
    )


def test_default_planning_prompt_does_not_contain_capability_block():
    """Default (no flavor arg) planning prompt must NOT contain 'locked until approval'."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert "locked until approval" not in got, (
        "capability block must not appear in the default (build) planning prompt"
    )


# ---------------------------------------------------------------------------
# 3. Build-flavor byte-stability is preserved after the E5 change
# ---------------------------------------------------------------------------


def test_build_planning_prompt_still_byte_identical_to_constant():
    """After E5: build-flavor planning output is still _PLANNING_DRIVER_PROMPT,
    byte for byte.  The capability block is agent-only and must not perturb build."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert got == _PLANNING_DRIVER_PROMPT, (
        "E5 change must not alter build-flavor planning output"
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

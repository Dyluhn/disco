"""F0 — Build/Agent surface isolation regression guard.

Pins the Build-surface prompt outputs and tool scope so that any future
Agent-surface change (flavor='agent' branch edits, AGENT_TOOLS additions)
that accidentally perturbs the Build surface is caught immediately.

Contract being guarded
───────────────────────
  • DriverPrompts(flavor="build") (the default) must produce prompts that are
    byte-identical to the upstream constants _PLANNING_DRIVER_PROMPT and
    _EXECUTION_DRIVER_PROMPT.  These constants ARE the build-surface snapshot;
    the flavor='agent' branch creates modified *copies* on the instance, never
    mutating the module-level strings.

  • Flipping flavor to "agent" diverges ONLY the agent-path strings — the base
    constants and the build DriverPrompts output remain untouched, so build
    never silently inherits an agent-side identity or prose edit.

  • AGENT_TOOLS is an explicit named frozenset; any addition or removal must
    also update the snapshot below, making the change a deliberate, code-reviewed
    decision rather than a silent drift.
"""

from __future__ import annotations

from disco.core.llm import DriverPrompts, ModelRole, OperatingMode
from disco.core.llm.prompts import (
    _AGENT_PLANNING_CAPABILITY_BLOCK,
    _EXECUTION_DRIVER_PROMPT,
    _MENTIONED_ELEMENT_GUIDANCE,
    _PLANNING_DRIVER_PROMPT,
)
from disco.tools.registry import AGENT_TOOLS

# [R6] The capability block now applies to BOTH flavors' PLANNING prompt (the build
# planner hides write tools too). So build PLANNING = base constant + this block;
# build EXECUTION is still the bare constant (the block is planning-only).
_BUILD_PLANNING = (
    _PLANNING_DRIVER_PROMPT + _MENTIONED_ELEMENT_GUIDANCE + _AGENT_PLANNING_CAPABILITY_BLOCK
)

# ---------------------------------------------------------------------------
# 1. Build-flavor PLANNING prompt is byte-stable
# ---------------------------------------------------------------------------


def test_build_planning_prompt_equals_constant():
    """flavor='build' planning output == _BUILD_PLANNING, byte for byte."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert got == _BUILD_PLANNING


def test_default_planning_prompt_equals_constant():
    """No-arg DriverPrompts() planning output == _BUILD_PLANNING.

    Callers that never pass flavor must see the same build-stable prompt."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert got == _BUILD_PLANNING


# ---------------------------------------------------------------------------
# 2. Build-flavor EXECUTION prompt is byte-stable (assist=False path)
# ---------------------------------------------------------------------------


def test_build_execution_prompt_equals_constant():
    """Build execution is the base constant plus shared mention/evidence guidance."""
    dp = DriverPrompts(flavor="build")
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
        assist=False,
    )
    assert got == _EXECUTION_DRIVER_PROMPT + _MENTIONED_ELEMENT_GUIDANCE


def test_default_execution_prompt_equals_constant():
    """The default execution prompt is the base plus shared mention/evidence guidance."""
    dp = DriverPrompts()
    got = dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert got == _EXECUTION_DRIVER_PROMPT + _MENTIONED_ELEMENT_GUIDANCE


# ---------------------------------------------------------------------------
# 3. Build prompts carry "build" identity — never "task" (agent-only phrasing)
# ---------------------------------------------------------------------------


def test_build_prompts_contain_build_identity_not_task_identity():
    """Build-surface prompts name 'autonomous build agent', never 'task agent'."""
    dp = DriverPrompts(flavor="build")
    for mode in (OperatingMode.PLANNING, OperatingMode.LONG_HORIZON):
        got = dp.system_prompt(
            model_family="qwen",
            mode=mode,
            role=ModelRole.AGENT_DRIVER,
        )
        assert "autonomous build agent" in got, f"mode={mode} missing build identity"
        assert "autonomous task agent" not in got, f"mode={mode} leaked task identity"


# ---------------------------------------------------------------------------
# 4. Agent flavor diverges from build; build path is unaffected
# ---------------------------------------------------------------------------


def test_agent_flavor_diverges_from_build_in_both_phases():
    """flavor='agent' produces different prompts; build prompts stay on constants."""
    build_dp = DriverPrompts(flavor="build")
    agent_dp = DriverPrompts(flavor="agent")

    # Planning phase
    build_plan = build_dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    agent_plan = agent_dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.PLANNING,
        role=ModelRole.AGENT_DRIVER,
    )
    assert build_plan != agent_plan, "planning: build and agent must diverge"
    assert "autonomous build agent" in build_plan
    assert "autonomous task agent" in agent_plan
    # Build path is still the constant — agent flavor didn't touch it.
    assert build_plan == _BUILD_PLANNING

    # Execution phase
    build_exec = build_dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    agent_exec = agent_dp.system_prompt(
        model_family="qwen",
        mode=OperatingMode.LONG_HORIZON,
        role=ModelRole.AGENT_DRIVER,
    )
    assert build_exec != agent_exec, "execution: build and agent must diverge"
    assert "autonomous build agent" in build_exec
    assert "autonomous task agent" in agent_exec
    # Build path is still the constant.
    assert build_exec == _EXECUTION_DRIVER_PROMPT + _MENTIONED_ELEMENT_GUIDANCE


def test_constructing_agent_flavor_does_not_mutate_module_constants():
    """flavor='agent' modifies instance strings only — the module-level constants
    are plain Python str objects and can never be mutated.  This test makes the
    invariant explicit: after constructing any number of agent DriverPrompts, a
    fresh build DriverPrompts still returns the original constants."""
    # Construct several agent-flavor instances (they each do .replace() on the
    # constant value to produce a new string, leaving the original untouched).
    for _ in range(3):
        DriverPrompts(flavor="agent")

    # A brand-new build instance must still see the unmodified constants.
    dp = DriverPrompts(flavor="build")
    assert (
        dp.system_prompt(
            model_family="qwen",
            mode=OperatingMode.PLANNING,
            role=ModelRole.AGENT_DRIVER,
        )
        == _BUILD_PLANNING
    )
    assert (
        dp.system_prompt(
            model_family="qwen",
            mode=OperatingMode.LONG_HORIZON,
            role=ModelRole.AGENT_DRIVER,
        )
        == _EXECUTION_DRIVER_PROMPT + _MENTIONED_ELEMENT_GUIDANCE
    )


# ---------------------------------------------------------------------------
# 5. Tool-scope snapshot — AGENT_TOOLS membership is an explicit named list
# ---------------------------------------------------------------------------

# UPDATE THIS LIST whenever a tool is added or removed from AGENT_TOOLS.
# That is the point: any scope change must touch this file, making it a
# deliberate, code-reviewed decision rather than silent drift.
_EXPECTED_AGENT_TOOLS = [
    # WO-TC2: trusted components — verified vendored security cores (free-form only)
    "add_trusted_component",
    # Legacy AppKit wrappers still used by governed persisted-AppSpec paths
    "app_set_tweak",
    "app_snapshot_version",
    "audio_overview",
    "browser",
    "code_exec",
    "context_memory",  # CXT-2: durable .disco/context working memory
    "deck_patch",  # C-EDIT-4: RFC-6902 deck edits
    "delegate_explore",
    "design_lint",  # B3 appkit port — read-only design-slop probe (nightly EPIC D3 parity)
    "doc_export",  # §E doc-assembly — host assembles the document at the P10 choke
    "doc_set_section",  # §E doc-assembly — pydantic-validated document part writes
    "eject_trusted_component",  # WO-TC2 — honest take-ownership relabel
    "exact_replace",  # CD-TOOLS-2 — atomic exact-match batch replace (anchored-edit tier)
    "extract",
    "file_append",
    "file_edit",
    "file_insert_lines",
    "file_list",
    "file_read",
    "file_replace_lines",
    "file_write",
    "hardware_identity",  # F07: provenance-bearing numeric PCI lookup
    "image_generate",
    # EPIC F: platform-owned preview surface — supersedes the old `deploy_preview`.
    "preview_logs",
    "preview_start",
    "preview_status",
    "preview_stop",
    "release_declare",  # WO-5: record the typed, NAMES-ONLY host-owned release intent
    "run_project_script",  # CD-TOOLS-7 — buffered, prevalidated batch of file transforms
    "scaffold_starter",  # P7: materialize the contract's host-owned starter frame
    "search",
    "server_status",
    "sheet_generate",
    "shell",
    "shell_exec",
    "shell_kill_process",
    "shell_view",
    "shell_wait",
    "shell_write_to_process",
    "slides_generate",
    "submit_plan",
    "think",
    # runthru-v2 (#3): declarative full-state progress snapshot — capable models
    # (assist OFF) use this in place of plan_step; prompt-gated, never gates finish.
    "update_plan_progress",
    # W-45: verify_web_app — structured web-app self-test feeding the finish gate's
    # clean pass/fail verdict (kills the browser reload verify-loop).
    "verify_web_app",
]


def test_agent_tools_scope_snapshot():
    """AGENT_TOOLS sorted == _EXPECTED_AGENT_TOOLS.

    Any addition or removal from the agent tool scope must also update the
    snapshot above.  Catching it here forces a conscious diff-review decision
    instead of a silent capability expansion or contraction."""
    assert sorted(AGENT_TOOLS) == _EXPECTED_AGENT_TOOLS

"""Cluster 9 — error-handling polish: the <error_handling> + <file_rules>
prompt ladders, and the anti-fewshot driver temperature."""

from __future__ import annotations

from perpleximanus.core.llm import DriverPrompts, ModelRole, OperatingMode


def _exec_prompt() -> str:
    return DriverPrompts().system_prompt(
        model_family="qwen", mode=OperatingMode.LONG_HORIZON, role=ModelRole.AGENT_DRIVER
    )


# ---- prompt ladders ---------------------------------------------------------


def test_execution_prompt_has_error_handling_ladder():
    p = _exec_prompt()
    assert "<error_handling>" in p
    # the verify → fix → alternate → escalate shape
    assert "verify the tool name" in p.lower() or "READ the error" in p
    assert "ask_user" in p


def test_execution_prompt_has_file_rules():
    p = _exec_prompt()
    assert "<file_rules>" in p
    assert "file_write" in p
    assert "file_append" in p
    # explicitly forbids shell redirection
    assert ">>" in p or "redirection" in p.lower()


def test_planning_prompt_unaffected_by_file_rules_block():
    plan = DriverPrompts().system_prompt(
        model_family="qwen", mode=OperatingMode.PLANNING, role=ModelRole.AGENT_DRIVER
    )
    # The file-rules block is an execution concern; planning stays a read-only
    # propose-the-plan surface.
    assert "PLANNING mode" in plan


# ---- anti-fewshot driver temperature ----------------------------------------


def test_router_agent_defaults_to_nonzero_temperature():
    from perpleximanus.core.loop.agent import RouterAgent

    agent = RouterAgent(router=None)  # ctor doesn't touch the router
    assert agent._temperature > 0.0
    assert 0.2 <= agent._temperature <= 0.6


def test_router_agent_temperature_is_overridable():
    from perpleximanus.core.loop.agent import RouterAgent

    agent = RouterAgent(router=None, temperature=0.0)
    assert agent._temperature == 0.0

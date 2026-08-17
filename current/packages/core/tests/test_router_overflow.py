"""Overflow policy tests — llm-router-contract.md §10.2.

DORMANT (v1.2 router lobotomy). Table-driven over the five default rules (§5.1),
local-only roles, stuck recovery through the router, cost caps (§5.2), and
escalation (§5.3) — ALL of which the deterministic router no longer does. The
policy and escalation paths are commented out in policy.py / routing.py / config.py;
these tests are kept (skipped) as the revival harness. Un-skip them, restore the
dormant code, and they should pass again. See llm-router-contract.md v1.2 appendix.
"""

from __future__ import annotations

# ruff: noqa: E402  (module-level skip must precede the now-dormant imports)
import pytest

pytest.skip(
    "DORMANT (v1.2): overflow policy + escalation + cost-driven routing are "
    "commented out; revive alongside the policy. See contract v1.2 appendix.",
    allow_module_level=True,
)

from disco.core import LLMMessage
from disco.core.llm import (
    BudgetExceeded,
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    CostTracker,
    Difficulty,
    LLMTransientError,
    ModelRole,
    OverflowSignal,
    Requirement,
    ThresholdOverflowPolicy,
)
from llm_fakes import FakeModelProvider, build_router, simple_config

CFG = simple_config()
POLICY = ThresholdOverflowPolicy()


def _decide(role, signal):
    return POLICY.decide(CapabilityProfile(role=role), signal, config=CFG)


# ---- the five rules, each in isolation --------------------------------------


def test_routine_no_signals_stays_local():
    path, triggers = _decide(ModelRole.AGENT_DRIVER, OverflowSignal(difficulty=Difficulty.ROUTINE))
    assert path == "local"
    assert triggers == []


def test_rule2_hard_step():
    path, triggers = _decide(ModelRole.AGENT_DRIVER, OverflowSignal(difficulty=Difficulty.HARD))
    assert path == "overflow"
    assert "hard_step" in triggers


def test_rule3_local_failure_escalation():
    path, triggers = _decide(
        ModelRole.AGENT_DRIVER,
        OverflowSignal(difficulty=Difficulty.ROUTINE, local_attempts_failed=1),
    )
    assert path == "overflow"
    assert "local_failed" in triggers


def test_rule4_stuck_recovery():
    path, triggers = _decide(
        ModelRole.AGENT_DRIVER,
        OverflowSignal(difficulty=Difficulty.ROUTINE, consecutive_tool_errors=2),
    )
    assert path == "overflow"
    assert "stuck_recovery" in triggers


def test_rule5_low_confidence():
    path, triggers = _decide(
        ModelRole.AGENT_DRIVER,
        OverflowSignal(difficulty=Difficulty.ROUTINE, last_local_confidence=0.3),
    )
    assert path == "overflow"
    assert "low_confidence" in triggers


def test_rule1_capability_gap():
    path, triggers = _decide(
        ModelRole.AGENT_DRIVER,
        OverflowSignal(difficulty=Difficulty.ROUTINE, requires=frozenset({Requirement.VISION})),
    )
    assert path == "overflow"
    assert "capability_gap" in triggers


# ---- local-only roles never overflow (unless config opts in) ----------------


def test_rag_answerer_is_local_only_even_when_hard():
    path, triggers = _decide(ModelRole.RAG_ANSWERER, OverflowSignal(difficulty=Difficulty.HARD))
    assert path == "local"
    assert triggers == []


def test_summarizer_is_local_only_even_when_hard():
    path, triggers = _decide(ModelRole.SUMMARIZER, OverflowSignal(difficulty=Difficulty.HARD))
    assert path == "local"


# ---- stuck recovery through the router (CallContext threading) --------------


async def test_stuck_recovery_routes_overflow_via_context():
    router, _sink, _ = build_router()
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="hi")],
    )
    resp = await router.complete(req, context=CallContext(consecutive_tool_errors=2))
    assert resp.routing.path == "overflow"
    assert "stuck_recovery" in resp.routing.overflow_triggers


# ---- cost governance (§5.2) -------------------------------------------------


async def test_soft_cap_suppresses_discretionary_but_keeps_necessity():
    cfg = simple_config().model_copy(update={"soft_budget_usd_global_daily": 1.0})
    cost = CostTracker()
    cost.add(5.0, conversation_id=None)  # already over the soft cap
    router, _sink, _ = build_router(config=cfg, cost_tracker=cost)

    # Discretionary (hard_step) is suppressed -> falls back to local.
    hard = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER, difficulty=Difficulty.HARD),
            messages=[LLMMessage(role="user", content="x")],
        )
    )
    assert hard.routing.path == "local"

    # Necessity (capability_gap) still overflows despite the soft cap.
    gap = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER, requirements=frozenset({Requirement.VISION})
            ),
            messages=[LLMMessage(role="user", content="x")],
        )
    )
    assert gap.routing.path == "overflow"
    assert "capability_gap" in gap.routing.overflow_triggers


async def test_hard_cap_raises_budget_exceeded_on_necessity_overflow():
    cfg = simple_config().model_copy(update={"hard_budget_usd_global_daily": 1.0})
    cost = CostTracker()
    cost.add(5.0, conversation_id=None)  # over the hard cap
    router, _sink, _ = build_router(config=cfg, cost_tracker=cost)
    # A capability gap would force overflow, but the hard cap disables it.
    with pytest.raises(BudgetExceeded):
        await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(
                    role=ModelRole.AGENT_DRIVER, requirements=frozenset({Requirement.VISION})
                ),
                messages=[LLMMessage(role="user", content="x")],
            )
        )


# ---- escalation (§5.3) ------------------------------------------------------


async def test_local_failure_escalates_to_overflow_and_increments_attempt():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))  # always fails
    overflow = FakeModelProvider("openrouter", text="frontier-recovered", cost_usd=0.02)
    router, _sink, _ = build_router(local=local, overflow=overflow)
    resp = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
            messages=[LLMMessage(role="user", content="x")],
        )
    )
    assert resp.routing.path == "overflow"
    assert resp.text == "frontier-recovered"
    assert resp.routing.attempt == 2
    assert "local_failed" in resp.routing.overflow_triggers

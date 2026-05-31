"""Coverage for shipped-but-initially-untested router pieces.

default_config (§7 starting assignments), the NLI stub (§9.2), per-conversation
cost tracking, and the escalation-blocked-by-hard-cap edge (§5.2/§5.3).
"""

from __future__ import annotations

import pytest
from llm_fakes import FakeModelProvider, build_router, simple_config
from perpleximanus.core import LLMMessage
from perpleximanus.core.llm import (
    BudgetExceeded,
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    CostTracker,
    Difficulty,
    LLMError,
    LLMTransientError,
    ModelEntry,
    ModelRole,
    RoleRouting,
    RouterConfig,
    StubNLIVerifier,
    default_config,
)

# ---- default_config (§7) ----------------------------------------------------


def test_default_config_has_all_roles_and_resolvable_models():
    cfg = default_config()
    assert set(cfg.roles) == set(ModelRole)  # all five roles assigned
    for routing in cfg.roles.values():
        assert routing.primary in cfg.models
        if routing.overflow is not None:
            assert routing.overflow in cfg.models
    # Only AGENT_DRIVER is overflow-eligible by default (§7 / §5.1).
    eligible = {r for r, rt in cfg.roles.items() if rt.overflow_eligible}
    assert eligible == {ModelRole.AGENT_DRIVER}
    assert cfg.entry_for("driver-local").provider == "ollama"


# ---- StubNLIVerifier (§9.2) -------------------------------------------------


def test_stub_nli_entailment_buckets():
    v = StubNLIVerifier()
    assert v.entail("the cat sat on the mat", "cat sat mat") == "entail"  # full overlap
    assert v.score("the cat sat on the mat", "cat sat mat") == pytest.approx(1.0)
    assert v.entail("totally unrelated premise", "xyz qqq zzz") == "contradict"  # ~0 overlap
    assert v.score("alpha beta gamma delta", "") == 0.0  # empty hypothesis


# ---- escalation blocked by a hard budget cap (§5.2 + §5.3) ------------------


async def test_local_failure_cannot_escalate_under_hard_cap():
    cfg = simple_config().model_copy(update={"hard_budget_usd_global_daily": 1.0})
    cost = CostTracker()
    cost.add(5.0, conversation_id=None)  # over the hard cap
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, _ = build_router(config=cfg, local=local, cost_tracker=cost)
    # Routine call routes local; the local transient would escalate to overflow,
    # but the hard cap forbids overflow → BudgetExceeded instead of escalating.
    with pytest.raises(BudgetExceeded):
        await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
                messages=[LLMMessage(role="user", content="x")],
            )
        )


async def test_hard_cap_with_only_discretionary_overflow_falls_back_to_local():
    """Hard cap + a HARD step (discretionary overflow, no capability need) → the
    router serves locally rather than raising (raise is reserved for necessity)."""
    cfg = simple_config().model_copy(update={"hard_budget_usd_global_daily": 1.0})
    cost = CostTracker()
    cost.add(5.0, conversation_id=None)
    router, _sink, _ = build_router(config=cfg, cost_tracker=cost)
    resp = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER, difficulty=Difficulty.HARD),
            messages=[LLMMessage(role="user", content="x")],
        )
    )
    assert resp.routing.path == "local"  # discretionary overflow dropped, not raised


# ---- per-conversation cost tracking (§5.2) ----------------------------------


async def test_per_conversation_cost_is_tracked():
    cost = CostTracker()
    router, _sink, _ = build_router(cost_tracker=cost)
    # A HARD step overflows to the frontier model (cost 0.02 in simple_config).
    await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER, difficulty=Difficulty.HARD),
            messages=[LLMMessage(role="user", content="x")],
        ),
        context=CallContext(conversation_id="conv-A"),
    )
    assert cost.conversation_cost("conv-A") == pytest.approx(0.02)
    assert cost.global_cost() == pytest.approx(0.02)
    assert cost.conversation_cost("conv-B") == 0.0  # isolated per conversation


# ---- misconfiguration: unconfigured role -----------------------------------


async def test_unconfigured_role_raises_llm_error():
    cfg = RouterConfig(
        models={
            "m": ModelEntry(model_id="m", provider="ollama", context_window=8192),
        },
        roles={ModelRole.RAG_ANSWERER: RoleRouting(primary="m")},  # AGENT_DRIVER missing
    )
    router, _sink, _ = build_router(config=cfg)
    with pytest.raises(LLMError):
        await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
                messages=[LLMMessage(role="user", content="x")],
            )
        )


# ---- NLI stub: the neutral (partial-overlap) bucket -------------------------


def test_stub_nli_neutral_bucket():
    v = StubNLIVerifier()
    # 1 of 4 hypothesis tokens overlap → 0.25, between the entail/contradict cuts.
    assert v.entail("the cat sat on the mat", "cat dog bird fish") == "neutral"

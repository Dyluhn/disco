"""Coverage for shipped router pieces (v1.2 deterministic).

default_config (§7 starting ASSIGNMENTS), the NLI stub (§9.2), the per-conversation
cost tracking that stays live as observability, and the passive hard-cap spend
backstop (§5.2). The escalation-under-hard-cap edges are DORMANT and live in
test_router_overflow.py (skipped).
"""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    BudgetExceeded,
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    CostTracker,
    ModelEntry,
    ModelRole,
    RouterConfig,
    StubNLIVerifier,
    default_config,
)
from llm_fakes import build_router, simple_config


def _driver_req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.AGENT_DRIVER),
        messages=[LLMMessage(role="user", content="x")],
    )


# ---- default_config (§7, v1.2 assignments) ----------------------------------


def test_default_config_resolves_every_role_to_a_known_model():
    cfg = default_config()
    # Every role resolves by direct lookup to a model that exists in the catalogue.
    for role in ModelRole:
        key = cfg.model_for(role)
        assert key in cfg.models, f"{role} -> {key} not in catalogue"
    # AGENT_DRIVER is unassigned on purpose → it resolves to default_model.
    assert cfg.model_for(ModelRole.AGENT_DRIVER) == cfg.default_model == "driver-local"
    # An explicitly-assigned role resolves to its assignment, not the default.
    assert cfg.model_for(ModelRole.RAG_ANSWERER) == "rag-local"
    assert cfg.entry_for("driver-local").provider == "qwen"
    assert cfg.entry_for("driver-local").base_url is not None  # wired to a live endpoint


def test_unassigned_role_falls_back_to_default_model():
    """A role absent from `assignments` deterministically resolves to
    default_model — no policy, no error."""
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="ollama", context_window=8192)},
        default_model="m",
        assignments={},  # nothing assigned
    )
    assert cfg.model_for(ModelRole.AGENT_DRIVER) == "m"
    assert cfg.model_for(ModelRole.NLI_VERIFIER) == "m"


# ---- StubNLIVerifier (§9.2) -------------------------------------------------


def test_stub_nli_entailment_buckets():
    v = StubNLIVerifier()
    assert v.entail("the cat sat on the mat", "cat sat mat") == "entail"  # full overlap
    assert v.score("the cat sat on the mat", "cat sat mat") == pytest.approx(1.0)
    assert v.entail("totally unrelated premise", "xyz qqq zzz") == "contradict"  # ~0 overlap
    assert v.score("alpha beta gamma delta", "") == 0.0  # empty hypothesis


def test_stub_nli_neutral_bucket():
    v = StubNLIVerifier()
    # 1 of 4 hypothesis tokens overlap → 0.25, between the entail/contradict cuts.
    assert v.entail("the cat sat on the mat", "cat dog bird fish") == "neutral"


# ---- per-conversation cost tracking stays live (§5.2 observability) ----------


async def test_per_conversation_cost_is_tracked():
    """Selecting the paid frontier model via the pill accrues its cost against the
    conversation. Cost tracking is observe-only now, but it still tracks."""
    cost = CostTracker()
    router, _sink, _ = build_router(cost_tracker=cost)
    await router.complete(
        _driver_req(),
        context=CallContext(conversation_id="conv-A", model_override="frontier"),
    )
    assert cost.conversation_cost("conv-A") == pytest.approx(0.02)  # frontier cost
    assert cost.global_cost() == pytest.approx(0.02)
    assert cost.conversation_cost("conv-B") == 0.0  # isolated per conversation


# ---- passive hard-cap spend backstop (§5.2) ---------------------------------


async def test_hard_cap_refuses_to_spend_passive_backstop():
    """v1.2: cost no longer drives selection, but a HARD cap still refuses to
    SPEND. Over the cap + a paid (pill-selected) model → BudgetExceeded."""
    cfg = simple_config().model_copy(update={"hard_budget_usd_global_daily": 1.0})
    cost = CostTracker()
    cost.add(5.0, conversation_id=None)  # already over the hard cap
    router, _sink, _ = build_router(config=cfg, cost_tracker=cost)
    with pytest.raises(BudgetExceeded):
        await router.complete(_driver_req(), context=CallContext(model_override="frontier"))


async def test_hard_cap_does_not_block_the_default_when_uncapped_role_is_free():
    """The backstop only bites accrued spend; a fresh conversation under no prior
    spend resolves and runs normally (determinism intact)."""
    router, _sink, _ = build_router()
    resp = await router.complete(_driver_req())
    assert resp.routing.path == "pinned"
    assert resp.model_used == "local-driver-q4"

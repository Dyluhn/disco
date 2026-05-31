"""Routing tests — llm-router-contract.md §10.1.

No name leak (RT2), requirement filtering (NoEligibleModel), and RoutingDecision
always emitted (RT4).
"""

from __future__ import annotations

import inspect

import pytest
from llm_fakes import build_router
from perpleximanus.core import LLMMessage
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    Difficulty,
    ModelEntry,
    ModelRole,
    NoEligibleModel,
    Requirement,
    RoleRouting,
    RouterConfig,
)

MSGS = [LLMMessage(role="user", content="do the thing")]


def _req(role=ModelRole.AGENT_DRIVER, **profile_kwargs) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=role, **profile_kwargs), messages=list(MSGS)
    )


# ---- RT2: no name leak ------------------------------------------------------


def test_complete_has_no_model_name_parameter():
    """RT2: callers cannot pin a model — there is no name parameter."""
    params = set(inspect.signature(DefaultLLMRouter.complete).parameters)
    assert "model" not in params
    assert params == {"self", "req", "context"}


async def test_difficulty_alone_changes_the_chosen_model():
    """Identical messages, different difficulty → different model, by capability
    routing (not a caller-chosen name)."""
    router, _sink, _ = build_router()
    routine = await router.complete(_req(difficulty=Difficulty.ROUTINE))
    hard = await router.complete(_req(difficulty=Difficulty.HARD))
    assert routine.routing.path == "local"
    assert hard.routing.path == "overflow"
    assert routine.routing.chosen_model != hard.routing.chosen_model


# ---- requirement filtering --------------------------------------------------


async def test_vision_requirement_routes_to_capable_overflow_model():
    router, _sink, _ = build_router()
    resp = await router.complete(_req(requirements=frozenset({Requirement.VISION})))
    assert resp.routing.path == "overflow"
    assert "capability_gap" in resp.routing.overflow_triggers
    assert resp.model_used == "frontier-xl"  # the only vision-capable model


async def test_no_eligible_model_raises_not_silent_fallback():
    """If no model satisfies the requirements, NoEligibleModel is raised."""
    cfg = RouterConfig(
        models={
            "local": ModelEntry(
                model_id="local-novision",
                provider="ollama",
                context_window=8192,
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            )
        },
        roles={ModelRole.AGENT_DRIVER: RoleRouting(primary="local", overflow_eligible=False)},
    )
    router, sink, _ = build_router(config=cfg)
    with pytest.raises(NoEligibleModel):
        await router.complete(_req(requirements=frozenset({Requirement.VISION})))
    # RT4: a terminal failure still emits exactly one decision.
    assert len(sink.decisions) == 1
    assert sink.decisions[0].overflow_triggers == ["no_eligible_model"]


# ---- RT4: a decision per call -----------------------------------------------


async def test_routing_decision_emitted_exactly_once_on_success():
    router, sink, _ = build_router()
    resp = await router.complete(_req())
    assert len(sink.decisions) == 1
    assert sink.decisions[0] == resp.routing  # same decision attached + logged
    assert resp.routing.path == "local"
    assert resp.routing.overflow_triggers == []


async def test_response_always_has_routing_and_usage():
    """RT1: the value the router returns always carries .routing and .usage."""
    router, _sink, _ = build_router()
    resp = await router.complete(_req())
    assert resp.routing is not None
    assert resp.usage is not None

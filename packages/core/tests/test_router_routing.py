"""Routing tests — llm-router-contract.md §10.1 (v1.2 deterministic).

No name leak through the request (RT2), deterministic config assignment, the
per-conversation override (model pill), capability fail-loud on mis-assignment,
and RoutingDecision always emitted (RT4).
"""

from __future__ import annotations

import inspect

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    Difficulty,
    LLMContentFiltered,
    ModelEntry,
    ModelRole,
    NoEligibleModel,
    Requirement,
    RouterConfig,
)
from llm_fakes import FakeModelProvider, build_router

MSGS = [LLMMessage(role="user", content="do the thing")]


def _req(role=ModelRole.AGENT_DRIVER, **profile_kwargs) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=role, **profile_kwargs), messages=list(MSGS)
    )


# ---- RT2: no name leak through the request ----------------------------------


def test_complete_has_no_model_name_parameter():
    """RT2: callers cannot pin a model THROUGH THE REQUEST — there is no name
    parameter. (The per-conversation pill is an explicit CallContext override, not
    a request field.)"""
    params = set(inspect.signature(DefaultLLMRouter.complete).parameters)
    assert "model" not in params
    assert params == {"self", "req", "context"}


# ---- deterministic assignment (v1.2) ----------------------------------------


async def test_difficulty_does_not_change_the_chosen_model():
    """v1.2: difficulty is ADVISORY — identical role, different difficulty → the
    SAME assigned model. (This is the inverse of the dormant intelligent router.)"""
    router, _sink, _ = build_router()
    routine = await router.complete(_req(difficulty=Difficulty.ROUTINE))
    hard = await router.complete(_req(difficulty=Difficulty.HARD))
    assert routine.routing.path == "pinned"
    assert hard.routing.path == "pinned"
    assert routine.routing.chosen_model == hard.routing.chosen_model
    assert routine.routing.overflow_triggers == []


async def test_role_resolves_to_assigned_model():
    """Each role resolves by direct config lookup: AGENT_DRIVER → default_model;
    RAG/SUMMARIZER → their explicit assignment (all "local" in simple_config)."""
    router, _sink, _ = build_router()
    for role in (ModelRole.AGENT_DRIVER, ModelRole.RAG_ANSWERER, ModelRole.SUMMARIZER):
        resp = await router.complete(_req(role=role))
        assert resp.model_used == "local-driver-q4"
        assert resp.routing.path == "pinned"
        assert resp.routing.reason == "config"


# ---- the per-conversation model pill (manual override) ----------------------


async def test_model_pill_override_selects_the_driver_model():
    """The main-screen pill pins the driver model for this conversation → path
    'manual', and it overrides the settings default."""
    router, _sink, _ = build_router()
    ctx = CallContext(model_override="frontier")
    resp = await router.complete(_req(), context=ctx)
    assert resp.model_used == "frontier-xl"
    assert resp.routing.path == "manual"
    assert resp.routing.reason == "config"


async def test_model_pill_overrides_driver_only_not_other_roles():
    """The pill leads the driver; non-driver roles still follow their settings
    assignment even when a driver override is present."""
    router, _sink, _ = build_router()
    ctx = CallContext(model_override="frontier")
    rag = await router.complete(_req(role=ModelRole.RAG_ANSWERER), context=ctx)
    assert rag.model_used == "local-driver-q4"  # settings assignment, not the pill
    assert rag.routing.path == "pinned"


# ---- reactive error surfacing (NO pre-call capability checking) -------------


async def test_no_capability_check_before_the_call():
    """v1.3 reactive: a VISION request against the text-only assignment is SENT —
    the router does NOT predict the model can't do it. The fake provider doesn't
    reject, so the call succeeds against the assigned model. (Capabilities are not
    inspected before the call.)"""
    local = FakeModelProvider("ollama", text="answered anyway")
    router, _sink, providers = build_router(local=local)
    resp = await router.complete(_req(requirements=frozenset({Requirement.VISION})))
    assert resp.model_used == "local-driver-q4"  # the assignment, sent as-is
    assert resp.text == "answered anyway"
    assert providers["ollama"].calls == 1  # the provider WAS called


async def test_provider_rejection_surfaces_with_real_content():
    """v1.3 reactive: if the assigned model rejects the input, the PROVIDER error
    propagates from complete() with its real content intact — not swallowed, not
    flattened into a generic 'model call failed'."""
    real_reason = "image input not supported by local-driver-q4"
    local = FakeModelProvider("ollama", raises=LLMContentFiltered(real_reason))
    router, _sink, _ = build_router(local=local)
    with pytest.raises(LLMContentFiltered) as excinfo:
        await router.complete(_req(requirements=frozenset({Requirement.VISION})))
    assert str(excinfo.value) == real_reason  # the real reason reaches the caller


async def test_vision_request_succeeds_when_a_vision_model_is_assigned():
    """Assign the vision-capable model to the driver (via the pill) → the VISION
    request runs against it."""
    router, _sink, _ = build_router()
    ctx = CallContext(model_override="frontier")
    resp = await router.complete(
        _req(requirements=frozenset({Requirement.VISION})), context=ctx
    )
    assert resp.model_used == "frontier-xl"
    assert resp.routing.path == "manual"


async def test_unknown_assignment_raises_not_silent():
    """An assignment pointing at a model absent from the catalogue fails loud."""
    cfg = RouterConfig(
        models={
            "local": ModelEntry(
                model_id="local-novision",
                provider="ollama",
                context_window=8192,
                capabilities=frozenset({Requirement.TOOL_CALLING}),
            )
        },
        default_model="ghost-model",  # not in `models`
    )
    router, sink, _ = build_router(config=cfg)
    with pytest.raises(NoEligibleModel):
        await router.complete(_req())
    assert len(sink.decisions) == 1
    assert sink.decisions[0].overflow_triggers == ["unknown_assignment"]


# ---- RT4: a decision per call -----------------------------------------------


async def test_routing_decision_emitted_exactly_once_on_success():
    router, sink, _ = build_router()
    resp = await router.complete(_req())
    assert len(sink.decisions) == 1
    assert sink.decisions[0] == resp.routing  # same decision attached + logged
    assert resp.routing.path == "pinned"
    assert resp.routing.overflow_triggers == []


async def test_response_always_has_routing_and_usage():
    """RT1: the value the router returns always carries .routing and .usage."""
    router, _sink, _ = build_router()
    resp = await router.complete(_req())
    assert resp.routing is not None
    assert resp.usage is not None

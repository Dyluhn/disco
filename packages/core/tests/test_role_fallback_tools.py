"""Tool-bearing auxiliary-role requests must not use the JSON/text-only fallback.

Regression tests for the provider-neutral role-fallback bug: the configured
auxiliary fallback accepts only JSON/text requirements (_role_fallback_target),
so an actual tool-bearing request must never dispatch there after transient
primary exhaustion. The guard must derive from the actual request
(CompletionRequest.tools), not from profile requirement labels or name
heuristics.

Covers router.complete and router.stream_complete with arbitrary identifiers
so familiar names gain no implicit behavior. Existing disabled/driver-role/
midstream no-replay cases stay in test_role_fallback.py and are not repeated.
"""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMTransientError,
    ModelEntry,
    ModelRole,
    Requirement,
    RouterConfig,
)
from disco.core.llm.config import ROLE_FALLBACK_PROVIDER_KEY, RoleFallbackSettings
from disco.core.llm.types import ToolSpec
from llm_fakes import FakeModelProvider, build_router

_ARBITRARY_PRIMARY_KEY = "arb-primary-key-9f2c"
_ARBITRARY_PRIMARY_MODEL = "arb-primary-model-9f2c"
_ARBITRARY_PRIMARY_PROVIDER = "arb-primary-transport-9f2c"
_ARBITRARY_FALLBACK_MODEL = "arb-aux-model-7f3a"
_ARBITRARY_FALLBACK_PROVIDER = "arb-aux-transport-7f3a"
_ARBITRARY_BASE_URL = "http://example.invalid/v1"
_ARBITRARY_TRANSIENT = "arb-transient-boom-5e1d"


def _arb_tool() -> ToolSpec:
    return ToolSpec(
        name="arb_lookup_4d2e",
        description="arbitrary lookup tool",
        parameters_schema={"type": "object", "properties": {}},
    )


def _arb_config() -> RouterConfig:
    return RouterConfig(
        models={
            _ARBITRARY_PRIMARY_KEY: ModelEntry(
                model_id=_ARBITRARY_PRIMARY_MODEL,
                provider=_ARBITRARY_PRIMARY_PROVIDER,
                context_window=65_536,
                capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
                family="arb-family",
            )
        },
        default_model=_ARBITRARY_PRIMARY_KEY,
        assignments={ModelRole.SUMMARIZER: _ARBITRARY_PRIMARY_KEY},
    )


def _router_with_arb_fallback(local: FakeModelProvider):
    config = _arb_config().model_copy(
        update={
            "role_fallback": RoleFallbackSettings(
                enabled=True,
                base_url=_ARBITRARY_BASE_URL,
                model=_ARBITRARY_FALLBACK_MODEL,
            )
        }
    )
    fallback = FakeModelProvider(_ARBITRARY_FALLBACK_PROVIDER, text="arb-aux-ok")
    router, sink, providers = build_router(config=config, local=local)
    # The shared helper supplies familiar provider keys; wire our arbitrary key explicitly.
    providers[_ARBITRARY_PRIMARY_PROVIDER] = local
    providers[ROLE_FALLBACK_PROVIDER_KEY] = fallback
    return router, sink, local, fallback


def _tool_req(requirements: frozenset[Requirement]) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.SUMMARIZER, requirements=requirements),
        messages=[LLMMessage(role="user", content="arb task with tools")],
        tools=[_arb_tool()],
    )


def _text_req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
        messages=[LLMMessage(role="user", content="arb text-only task")],
    )


async def test_complete_actual_tools_absent_requirements_blocks_fallback():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    with pytest.raises(LLMTransientError) as excinfo:
        await router.complete(_tool_req(frozenset()))

    assert _ARBITRARY_TRANSIENT in str(excinfo.value)
    assert primary.calls == 8
    assert fallback.calls == 0


async def test_complete_actual_tools_json_only_requirements_blocks_fallback():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    with pytest.raises(LLMTransientError) as excinfo:
        await router.complete(_tool_req(frozenset({Requirement.JSON_MODE})))

    assert _ARBITRARY_TRANSIENT in str(excinfo.value)
    assert primary.calls == 8
    assert fallback.calls == 0


async def test_stream_actual_tools_absent_requirements_blocks_fallback():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    with pytest.raises(LLMTransientError) as excinfo:
        async for _chunk in router.stream_complete(_tool_req(frozenset())):
            pass

    assert _ARBITRARY_TRANSIENT in str(excinfo.value)
    assert primary.calls == 8
    assert fallback.calls == 0


async def test_stream_actual_tools_json_only_requirements_blocks_fallback():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    with pytest.raises(LLMTransientError) as excinfo:
        async for _chunk in router.stream_complete(_tool_req(frozenset({Requirement.JSON_MODE}))):
            pass

    assert _ARBITRARY_TRANSIENT in str(excinfo.value)
    assert primary.calls == 8
    assert fallback.calls == 0


async def test_complete_toolfree_control_still_falls_back():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    resp = await router.complete(_text_req())

    assert resp.text == "arb-aux-ok"
    assert resp.model_used == _ARBITRARY_FALLBACK_MODEL
    assert primary.calls == 8
    assert fallback.calls == 1


async def test_stream_toolfree_control_still_falls_back():
    local = FakeModelProvider(
        _ARBITRARY_PRIMARY_PROVIDER, raises=LLMTransientError(_ARBITRARY_TRANSIENT)
    )
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    chunks = [chunk async for chunk in router.stream_complete(_text_req())]

    assert "".join(chunk.delta_text for chunk in chunks) == "arb-aux-ok"
    assert primary.calls == 8
    assert fallback.calls == 1


async def test_complete_tool_bearing_primary_success_not_refused():
    local = FakeModelProvider(_ARBITRARY_PRIMARY_PROVIDER, text="arb-primary-ok")
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    resp = await router.complete(_tool_req(frozenset()))

    assert resp.text == "arb-primary-ok"
    assert resp.model_used == _ARBITRARY_PRIMARY_MODEL
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_stream_tool_bearing_primary_success_not_refused():
    local = FakeModelProvider(_ARBITRARY_PRIMARY_PROVIDER, text="arb-primary-ok")
    router, _sink, primary, fallback = _router_with_arb_fallback(local=local)

    chunks = [chunk async for chunk in router.stream_complete(_tool_req(frozenset()))]

    assert "".join(chunk.delta_text for chunk in chunks) == "arb-primary-ok"
    assert primary.calls == 1
    assert fallback.calls == 0

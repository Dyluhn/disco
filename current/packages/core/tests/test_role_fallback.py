"""F4 — auxiliary-role local model fallback on transient primary failures."""

from __future__ import annotations

import pytest
from disco.core import LLMMessage
from disco.core.inspect import registry
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    LLMTransientError,
    ModelRole,
    Requirement,
)
from disco.core.llm.config import ROLE_FALLBACK_PROVIDER_KEY, RoleFallbackSettings
from llm_fakes import FakeModelProvider, build_router, simple_config


def _req(role: ModelRole) -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=role),
        messages=[LLMMessage(role="user", content="x")],
    )


def _fallback_config(*, enabled: bool = True):
    return simple_config().model_copy(
        update={
            "role_fallback": RoleFallbackSettings(
                enabled=enabled,
                base_url="http://localhost:8080/v1",
                model="fallback-llama",
            )
        }
    )


def _router_with_fallback(*, enabled: bool = True, local: FakeModelProvider):
    fallback = FakeModelProvider("role_fallback", text="fallback-ok")
    router, sink, providers = build_router(config=_fallback_config(enabled=enabled), local=local)
    providers[ROLE_FALLBACK_PROVIDER_KEY] = fallback
    return router, sink, local, fallback


async def test_aux_role_transient_exhaustion_uses_role_fallback():
    local = FakeModelProvider("ollama", raises=LLMTransientError("429"))
    router, sink, primary, fallback = _router_with_fallback(local=local)

    resp = await router.complete(_req(ModelRole.SUMMARIZER))

    assert resp.text == "fallback-ok"
    assert resp.model_used == "fallback-llama"
    assert primary.calls == 5
    assert fallback.calls == 1
    assert len(sink.decisions) == 1
    assert sink.decisions[0].path == "role_fallback"
    assert sink.decisions[0].reason == "role_fallback after local-driver-q4"
    assert sink.decisions[0].overflow_triggers == ["original_model:local-driver-q4"]


async def test_inspect_call_ordinal_stays_monotonic_across_role_fallback(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    local = FakeModelProvider("ollama", raises=LLMTransientError("hidden"))
    router, sink, primary, fallback = _router_with_fallback(local=local)

    response = await router.complete(
        _req(ModelRole.SUMMARIZER),
        context=CallContext(conversation_id="fallback-ordinal"),
    )

    assert response.routing is not None and response.routing.attempt == 1
    assert sink.decisions[0].attempt == 1
    assert primary.calls == 5 and fallback.calls == 1
    rows = registry().snapshot("fallback-ordinal")["model_attempts"]
    assert [row["call_ordinal"] for row in rows] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]
    assert [row["attempt"] for row in rows][-2:] == [1, 1]


async def test_driver_role_never_uses_role_fallback():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, primary, fallback = _router_with_fallback(local=local)

    with pytest.raises(LLMTransientError):
        await router.complete(_req(ModelRole.AGENT_DRIVER))

    assert primary.calls == 5
    assert fallback.calls == 0


async def test_vision_verifier_never_falls_back_to_json_only_auxiliary_model():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, primary, fallback = _router_with_fallback(local=local)
    req = _req(ModelRole.VERIFIER).model_copy(
        update={
            "profile": CapabilityProfile(
                role=ModelRole.VERIFIER,
                requirements=frozenset({Requirement.JSON_MODE, Requirement.VISION}),
            )
        }
    )

    with pytest.raises(LLMTransientError):
        await router.complete(req)

    assert primary.calls == 5
    assert fallback.calls == 0


async def test_image_payload_itself_blocks_json_only_role_fallback():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    config = _fallback_config()
    local_entry = config.models[config.default_model]
    config = config.model_copy(
        update={
            "models": {
                **config.models,
                config.default_model: local_entry.model_copy(
                    update={"capabilities": local_entry.capabilities | {Requirement.VISION}}
                ),
            }
        }
    )
    fallback = FakeModelProvider("role_fallback", text="fallback-ok")
    router, _sink, providers = build_router(config=config, local=local)
    providers[ROLE_FALLBACK_PROVIDER_KEY] = fallback
    req = _req(ModelRole.VERIFIER).model_copy(
        update={
            "messages": [
                LLMMessage(
                    role="user",
                    content="inspect pixels",
                    images=["data:image/png;base64,YQ=="],
                )
            ]
        }
    )

    with pytest.raises(LLMTransientError):
        await router.complete(req)

    assert local.calls == 5
    assert fallback.calls == 0


async def test_fallback_disabled_keeps_current_transient_behavior():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, _sink, primary, fallback = _router_with_fallback(enabled=False, local=local)

    with pytest.raises(LLMTransientError):
        await router.complete(_req(ModelRole.SUMMARIZER))

    assert primary.calls == 5
    assert fallback.calls == 0


async def test_stream_aux_role_falls_back_before_any_chunk_is_yielded():
    local = FakeModelProvider("ollama", raises=LLMTransientError("flaky"))
    router, sink, primary, fallback = _router_with_fallback(local=local)

    chunks = [chunk async for chunk in router.stream_complete(_req(ModelRole.SUMMARIZER))]

    assert "".join(chunk.delta_text for chunk in chunks) == "fallback-ok"
    assert chunks[-1].final is not None
    assert chunks[-1].final.routing is not None
    assert chunks[-1].final.routing.path == "role_fallback"
    assert primary.calls == 5
    assert fallback.calls == 1
    assert sink.decisions[0].path == "role_fallback"


async def test_stream_midstream_transient_is_not_replayed_with_fallback():
    local = FakeModelProvider(
        "ollama",
        text="primary-stream",
        raises=LLMTransientError("midstream"),
        stream_raises_after_chunks=1,
    )
    router, _sink, primary, fallback = _router_with_fallback(local=local)

    with pytest.raises(LLMTransientError):
        async for _chunk in router.stream_complete(_req(ModelRole.SUMMARIZER)):
            pass

    assert primary.calls == 1
    assert fallback.calls == 0

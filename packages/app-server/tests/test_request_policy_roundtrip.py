"""The saved settings, reloaded runtime and actual wire request share one policy."""

import json
from pathlib import Path

import httpx
import pytest
from disco.app_server.config.dtos import ModelUpsert
from disco.app_server.config_state import ConfigState
from disco.core import LLMMessage, SkillStore
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    ConfigStore,
    ModelRole,
    SecretBox,
    SecretStore,
)
from disco.core.llm.request_policy import RequestPolicy
from disco.core.llm.wiring import build_providers


async def test_save_reload_edit_and_wire_policy_for_an_unknown_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "policy-test-secret")
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("policy-test-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    policy = RequestPolicy(reasoning_disabled={"reasoning_effort": "none"})
    upsert = ModelUpsert(
        id="opaque-model",
        model_id="unknown-build-9000",
        base_url="https://unknown.example/v1",
        requires_api_key=False,
        request_policy=policy,
    )
    saved = state.models.add_model(upsert)
    assert next(m for m in saved if m.id == upsert.id).request_policy == policy
    # An older client's unrelated edit must not erase options it does not know.
    old_client = ModelUpsert.model_validate(upsert.model_dump(exclude={"request_policy"}))
    state.models.update_model(upsert.id, old_client)
    loaded = ConfigStore(tmp_path / "config.json").load()
    entry = loaded.models[upsert.id]
    assert entry.request_policy == policy
    providers = build_providers(loaded, origin_approved=lambda *_: True)
    provider = providers[entry.provider]
    seen = []
    capture = (
        Path(__file__).parents[2] / "core/tests/fixtures/ollama-20260922/deepseek-v4.1-flash.sse"
    )

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(
            200, content=capture.read_bytes(), headers={"content-type": "text/event-stream"}
        )

    provider._transport = httpx.MockTransport(handler)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content='Return exactly {"ok": true} as JSON.')],
        enable_thinking=False,
    )
    response = await provider.complete(req, model=entry.model_id)
    assert json.loads(response.text) == {"ok": True}
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[0]["model"] == "unknown-build-9000"
    state.models.update_model(
        upsert.id, upsert.model_copy(update={"request_policy": RequestPolicy()})
    )
    assert (
        ConfigStore(tmp_path / "config.json").load().models[upsert.id].request_policy
        == RequestPolicy()
    )


def test_conflicting_alias_policies_do_not_silently_select_the_first():
    from disco.core.llm import ModelEntry, RouterConfig

    a = ModelEntry(
        model_id="same",
        provider="endpoint",
        context_window=8192,
        base_url="https://unknown.example/v1",
        requires_api_key=False,
        request_policy=RequestPolicy(reasoning_disabled={"switch": False}),
    )
    b = a.model_copy(update={"request_policy": RequestPolicy(reasoning_disabled={"effort": 0})})
    config = RouterConfig(models={"a": a, "b": b}, default_model="a")
    with pytest.raises(ValueError, match="Conflicting request policies"):
        build_providers(config, origin_approved=lambda *_: True)

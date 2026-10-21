"""Missing catalogue selections must never inherit another driver's readiness."""

from __future__ import annotations

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import CompletionResponse, ConfigStore, ModelEntry, RouterConfig, TokenUsage


class _CountingProvider:
    name = "fixture"

    def __init__(self):
        self.models: list[str] = []

    async def complete(self, req, *, model):
        self.models.append(model)
        return CompletionResponse(
            text="ok",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    def supports(self, requirement, *, model):
        return True


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    import disco.agent_server.driver_runtime as module

    provider = _CountingProvider()
    monkeypatch.setattr(module, "build_providers", lambda *_args, **_kwargs: {"fixture": provider})
    config = ConfigStore(tmp_path / "router.json")
    config.save(
        RouterConfig(
            models={
                "primary": ModelEntry(
                    model_id="opaque-main", provider="fixture", context_window=8192
                ),
                "temporary": ModelEntry(
                    model_id="opaque-second", provider="fixture", context_window=8192
                ),
            },
            default_model="primary",
        )
    )
    rt = ConversationRuntime(SqliteEventStore(":memory:"), config_store=config)
    yield rt, config, provider
    await rt._driver_preflight.aclose()


async def test_missing_override_names_the_actual_selection_without_dispatch(runtime):
    rt, _config, provider = runtime
    rt.settings.set_model_override("old", "removed-choice")
    reason = await rt._preflight_driver("old")
    assert reason is not None and "Driver 'removed-choice'" in reason
    assert "Choose a configured model" in reason
    assert "primary" not in reason
    assert provider.models == []
    assert not rt._driver_preflight._proven


async def test_missing_override_does_not_poison_valid_default_shared_readiness(runtime):
    rt, _config, provider = runtime
    rt.settings.set_model_override("old", "removed-choice")
    assert await rt._preflight_driver("old") is not None
    assert await rt._preflight_driver("new") is None
    assert provider.models == ["opaque-main"]


async def test_warm_default_cache_cannot_accept_missing_conversation_override(runtime):
    rt, _config, provider = runtime
    assert await rt._preflight_driver("new") is None
    rt.settings.set_model_override("old", "removed-choice")
    reason = await rt._preflight_driver("old")
    assert reason is not None and "removed-choice" in reason
    assert provider.models == ["opaque-main"]
    assert not any(proof[0] == "old" for proof in rt._driver_preflight._proven)
    # Explicit recovery still uses the valid shared readiness, without a fallback.
    rt.settings.set_model_override("old", "primary")
    assert await rt._preflight_driver("old") is None
    assert provider.models == ["opaque-main"]


async def test_removed_previously_ready_model_is_rejected_before_its_own_cache(runtime):
    rt, config, provider = runtime
    rt.settings.set_model_override("old", "temporary")
    assert await rt._preflight_driver("old") is None
    assert provider.models == ["opaque-second"]
    config.remove_model("temporary")
    reason = await rt._preflight_driver("old")
    assert reason is not None and "Driver 'temporary'" in reason
    assert provider.models == ["opaque-second"]
    assert rt.settings._get_model_override("old") == "temporary"

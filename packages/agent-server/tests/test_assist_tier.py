import os
from unittest.mock import MagicMock, patch

from disco.agent_server.runtime import ConversationRuntime
from disco.core.llm import ModelExecutionPolicy
from disco.core.llm.types import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.loop.engine import AgentLoop
from disco.tools.anatomy import ToolContext


def test_completion_request_assist_field():
    profile = CapabilityProfile(role=ModelRole.AGENT_DRIVER)
    req = CompletionRequest(profile=profile, messages=[])
    assert req.assist is False


def test_tool_context_assist_field():
    sandbox = MagicMock()
    ctx = ToolContext(
        sandbox=sandbox,
        message_index=1,
        call_id="c1",
        request_id="r1",
        workspace_path="",
        timeout_s=10,
        capabilities=[],
        owner_id="",
        conversation_id="",
    )
    assert ctx.assist is False


def test_agent_loop_assist_field():
    """AgentLoop._assist is now a property delegating to _model_policy.assist.
    A weak ModelExecutionPolicy makes _assist True (the existing collaborator
    contract — agent.py / driver.py still read self._loop._assist)."""
    loop = AgentLoop(
        conversation_id="conv1",
        store=MagicMock(),
        agent=MagicMock(),
        executor=MagicMock(),
        router=MagicMock(),
        analyzer=MagicMock(),
        policy=MagicMock(),
        condenser=MagicMock(),
        summarizer=MagicMock(),
        mode=MagicMock(),
        model_policy=ModelExecutionPolicy(tier="weak"),
    )
    assert loop._assist is True


def _model_entry(base_url: str) -> MagicMock:
    """Minimal model-entry mock.

    MagicMock auto-creates ANY attribute access as a truthy MagicMock, so plain
    `MagicMock(base_url=…)` leaves `.tier` as a non-None MagicMock.  That causes
    `_effective_policy` to take the `elif entry_tier is not None` branch, skip the
    hosting heuristic, and set tier=MagicMock() (which compares False to "weak").

    Fix: explicitly set `tier=None` and `capabilities=frozenset()` so the policy
    resolver falls through to the hosting heuristic — which is what these tests
    actually exercise."""
    entry = MagicMock(base_url=base_url, tier=None)
    entry.capabilities = frozenset()
    return entry


def test_runtime_effective_assist_default(tmp_path):
    """Assist is EXPLICIT-toggle-only (Dylan's requirement): NEVER auto-enabled by
    hosting. Every model — local, LAN, mDNS, cloud — defaults to assist OFF; the only
    ways in are the per-conversation UI toggle or an explicit ModelEntry.tier='weak'."""
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        rt = ConversationRuntime(store=MagicMock())

        config = MagicMock()
        rt._config_store.load = MagicMock(return_value=config)
        rt._router_now = MagicMock(side_effect=AssertionError("metadata read wired providers"))

        # Local / LAN / mDNS / cloud — ALL default OFF now (no hosting auto-weak; a
        # capable local model like Qwen 27B is not sandbagged for running on localhost).
        for cid, url in [
            ("c1", "http://127.0.0.1:8080/v1"),
            ("c2", "https://api.anthropic.com/v1"),
            ("c3", "https://openrouter.ai/api/v1/local"),
            ("c4", "http://192.168.1.231:18080/v1"),
            ("c5", "http://workstation.local:18080/v1"),
        ]:
            config.model_for.return_value = "m"
            config.models = {"m": _model_entry(url)}
            assert rt._settings._effective_assist(cid) is False, f"{url} should default OFF"

        # The explicit per-conversation toggle is the ONLY default-path way in: setting
        # it flips assist ON even for a local model (where the old heuristic auto-ON'd).
        config.model_for.return_value = "m"
        config.models = {"m": _model_entry("http://127.0.0.1:8080/v1")}
        rt.set_assist("c6", True)
        assert rt._settings._effective_assist("c6") is True
        rt.set_assist("c6", False)
        assert rt._settings._effective_assist("c6") is False

        # An explicit ModelEntry.tier='weak' in config still enables it (config escape hatch).
        weak_entry = _model_entry("https://api.anthropic.com/v1")
        weak_entry.tier = "weak"
        config.model_for.return_value = "weak-cfg"
        config.models = {"weak-cfg": weak_entry}
        assert rt._settings._effective_assist("c7") is True


def test_create_body_sets_assist_and_state_extras(tmp_path):
    """G1 gap: the create-body `assist` flag must wire to set_assist, and the
    /state response must surface extras.assist when is_assist — the two stated
    MUST-DOs the adversarial gate flagged as untested. Driven through the real
    ASGI app (create_app + TestClient), not the runtime in isolation."""
    from disco.agent_server.app import create_app
    from disco.core import SqliteEventStore
    from fastapi.testclient import TestClient

    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        store = SqliteEventStore(":memory:")
        rt = ConversationRuntime(store=store)
        # is_assist default is irrelevant here — create-body sets it explicitly.
        app = create_app(store, runtime=rt)
        client = TestClient(app)

        # create-body assist=True → set_assist wired → is_assist True
        cid = client.post("/conversations", json={"surface": "build", "assist": True}).json()[
            "conversation_id"
        ]
        assert rt.is_assist(cid) is True
        # /state surfaces extras.assist without constructing live providers. The
        # soak polls this route continuously; provider wiring here caused six warning
        # lines and secret/origin work per poll (15,864 warnings in one partial wave).
        rt._router_now = MagicMock(side_effect=AssertionError("state poll wired providers"))
        for _ in range(3):
            st = client.get(f"/conversations/{cid}/state").json()
            assert st["extras"].get("assist") is True
        rt._router_now.assert_not_called()

        # create-body assist=False → explicit OFF must STICK even where the
        # probed default would be ON. G3 flagged this: the original test only
        # ever sent assist=True, so `body.assist is not None` could have ignored
        # an explicit False and the override-honoring path would pass silently.
        cid_off = client.post("/conversations", json={"surface": "build", "assist": False}).json()[
            "conversation_id"
        ]
        assert rt.is_assist(cid_off) is False


def test_runtime_assist_explicit_override(tmp_path):
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        rt = ConversationRuntime(store=MagicMock())

        config = MagicMock()
        rt._config_store.load = MagicMock(return_value=config)
        rt._router_now = MagicMock(side_effect=AssertionError("metadata read wired providers"))

        # Set up local model which would default to True
        config.model_for.return_value = "local-model"
        entry_local = MagicMock(base_url="http://127.0.0.1:8080/v1")
        config.models = {"local-model": entry_local}

        # But explicitly set assist to False
        rt.set_assist("c1", False)
        assert rt.is_assist("c1") is False
        assert rt._settings._effective_assist("c1") is False

        # Set up cloud model which would default to False
        config.model_for.return_value = "cloud-model"
        entry_cloud = MagicMock(base_url="https://api.openai.com/v1")
        config.models = {"cloud-model": entry_cloud}

        # Explicitly set assist to True
        rt.set_assist("c2", True)
        assert rt.is_assist("c2") is True
        assert rt._settings._effective_assist("c2") is True

        # Check sidecar was created and is valid
        assert os.path.exists(rt._settings._assist_path)
        rt2 = ConversationRuntime(store=MagicMock())
        assert rt2._settings._assist["c1"] is False
        assert rt2._settings._assist["c2"] is True


def test_metadata_policy_uses_injected_router_config_without_provider_rebuild() -> None:
    config = MagicMock()
    config.model_for.return_value = "weak-cfg"
    weak_entry = _model_entry("https://provider.example/v1")
    weak_entry.tier = "weak"
    config.models = {"weak-cfg": weak_entry}
    router = MagicMock()
    router._config = config
    rt = ConversationRuntime(store=MagicMock(), router=router)
    rt._config_store.load = MagicMock(side_effect=AssertionError("ignored injected config"))

    assert rt.is_assist("c1") is True
    rt._config_store.load.assert_not_called()


def test_root5_effective_driver_endpoint_honors_override():
    """ROOT-5: LLM-using tools (slides_generate) must author with the conversation's
    PICKED model. _effective_driver_endpoint resolves the OVERRIDE-aware AGENT_DRIVER
    entry → (base_url, model_id, api_key_env), not the global default."""
    from disco.core.llm.config import ModelEntry

    rt = ConversationRuntime(store=MagicMock())
    config = MagicMock()
    rt._config_store.load = MagicMock(return_value=config)
    rt._router_now = MagicMock(side_effect=AssertionError("metadata read wired providers"))
    rt._settings._model_overrides = {"c1": "or-deepseek"}
    entry = ModelEntry(
        model_id="deepseek/deepseek-v4-pro",
        provider="openrouter",
        context_window=64_000,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    )
    config.model_for.return_value = "or-deepseek"
    config.models = {"or-deepseek": entry}
    rt._settings._routing.origin_approved = MagicMock(return_value=True)

    ep = rt._settings._effective_driver_endpoint("c1")
    assert ep == ("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-pro", "OPENROUTER_API_KEY")
    # the resolver consulted model_for with the per-conversation override
    config.model_for.assert_called_with(ModelRole.AGENT_DRIVER, override="or-deepseek")
    # no live base_url ⇒ None (the tool then falls back to the global resolver)
    config.models = {"or-deepseek": ModelEntry(model_id="x", provider="p", context_window=1)}
    assert rt._settings._effective_driver_endpoint("c1") is None

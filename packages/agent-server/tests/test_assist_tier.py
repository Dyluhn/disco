import pytest
import os
from unittest.mock import MagicMock, patch
from disco.agent_server.runtime import ConversationRuntime
from disco.core.loop.engine import AgentLoop
from disco.core.llm.types import CompletionRequest
from disco.core.llm.types import CapabilityProfile, ModelRole
from disco.tools.anatomy import ToolContext

def test_completion_request_assist_field():
    profile = CapabilityProfile(role=ModelRole.AGENT_DRIVER)
    req = CompletionRequest(profile=profile, messages=[])
    assert req.assist is False

def test_tool_context_assist_field():
    sandbox = MagicMock()
    ctx = ToolContext(sandbox=sandbox, message_index=1, call_id="c1", request_id="r1", workspace_path="", timeout_s=10, capabilities=[], owner_id="", conversation_id="")
    assert ctx.assist is False

def test_agent_loop_assist_field():
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
        assist=True
    )
    assert loop._assist is True

def test_runtime_effective_assist_default(tmp_path):
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        rt = ConversationRuntime(store=MagicMock())

        # Mock the router and config
        router = MagicMock()
        rt._router_now = MagicMock(return_value=router)

        # Local base url -> Default ON
        router._config.model_for.return_value = "local-model"
        entry_local = MagicMock(base_url="http://127.0.0.1:8080/v1")
        router._config.models = {"local-model": entry_local}
        
        assert rt._effective_assist("c1") is True

        # Cloud base url -> Default OFF
        router._config.model_for.return_value = "cloud-model"
        entry_cloud = MagicMock(base_url="https://api.anthropic.com/v1")
        router._config.models = {"cloud-model": entry_cloud}
        
        assert rt._effective_assist("c2") is False

        # OpenRouter URL with local in it just in case -> Default OFF
        router._config.model_for.return_value = "or-model"
        entry_or = MagicMock(base_url="https://openrouter.ai/api/v1/local")
        router._config.models = {"or-model": entry_or}

        assert rt._effective_assist("c3") is False

        # LAN IP (192.168.x) -> local -> Default ON (gate-policy positive case)
        router._config.model_for.return_value = "lan-model"
        router._config.models = {"lan-model": MagicMock(base_url="http://192.168.1.231:18080/v1")}
        assert rt._effective_assist("c4") is True

        # *.local mDNS host -> local -> Default ON (gate-policy positive case)
        router._config.model_for.return_value = "mdns-model"
        router._config.models = {"mdns-model": MagicMock(base_url="http://workstation.local:18080/v1")}
        assert rt._effective_assist("c5") is True


def test_create_body_sets_assist_and_state_extras(tmp_path):
    """G1 gap: the create-body `assist` flag must wire to set_assist, and the
    /state response must surface extras.assist when is_assist — the two stated
    MUST-DOs the adversarial gate flagged as untested. Driven through the real
    ASGI app (create_app + TestClient), not the runtime in isolation."""
    from fastapi.testclient import TestClient
    from disco.agent_server.app import create_app
    from disco.core import SqliteEventStore

    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        store = SqliteEventStore(":memory:")
        rt = ConversationRuntime(store=store)
        # is_assist default is irrelevant here — create-body sets it explicitly.
        app = create_app(store, runtime=rt)
        client = TestClient(app)

        # create-body assist=True → set_assist wired → is_assist True
        cid = client.post("/conversations", json={"surface": "build", "assist": True}).json()["conversation_id"]
        assert rt.is_assist(cid) is True
        # /state surfaces extras.assist
        st = client.get(f"/conversations/{cid}/state").json()
        assert st["extras"].get("assist") is True

        # create-body assist=False → explicit OFF must STICK even where the
        # probed default would be ON. G3 flagged this: the original test only
        # ever sent assist=True, so `body.assist is not None` could have ignored
        # an explicit False and the override-honoring path would pass silently.
        cid_off = client.post(
            "/conversations", json={"surface": "build", "assist": False}
        ).json()["conversation_id"]
        assert rt.is_assist(cid_off) is False


def test_runtime_assist_explicit_override(tmp_path):
    with patch.dict("os.environ", {"PMX_DB": str(tmp_path / "disco.db")}):
        rt = ConversationRuntime(store=MagicMock())

        router = MagicMock()
        rt._router_now = MagicMock(return_value=router)

        # Set up local model which would default to True
        router._config.model_for.return_value = "local-model"
        entry_local = MagicMock(base_url="http://127.0.0.1:8080/v1")
        router._config.models = {"local-model": entry_local}
        
        # But explicitly set assist to False
        rt.set_assist("c1", False)
        assert rt.is_assist("c1") is False
        assert rt._effective_assist("c1") is False

        # Set up cloud model which would default to False
        router._config.model_for.return_value = "cloud-model"
        entry_cloud = MagicMock(base_url="https://api.openai.com/v1")
        router._config.models = {"cloud-model": entry_cloud}

        # Explicitly set assist to True
        rt.set_assist("c2", True)
        assert rt.is_assist("c2") is True
        assert rt._effective_assist("c2") is True
        
        # Check sidecar was created and is valid
        assert os.path.exists(rt._assist_path)
        rt2 = ConversationRuntime(store=MagicMock())
        assert rt2._assist["c1"] is False
        assert rt2._assist["c2"] is True

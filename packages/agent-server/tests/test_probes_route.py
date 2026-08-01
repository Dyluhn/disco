"""T4.3 / T4.4 / T4.5 — the agent-server Settings test probes (TTS / image / MCP).

These reuse the real provider clients; the unit tests drive the deterministic
branches (disabled / misconfigured / procedural-fallback / no-input) live, and
monkeypatch the remote synth for the paid-TTS happy path so no network is needed.
Each probe must return ok=False at HTTP 200 for an expected failure — never a 500.
"""

from __future__ import annotations

import pytest
from disco.agent_server import create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore
from disco.core.llm.config import ImageGenSettings, TtsSettings
from fastapi.testclient import TestClient


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """Point both servers' shared ConfigStore + SecretStore at temp files."""
    p = tmp_path / "config.json"
    monkeypatch.setenv("DISCO_CONFIG", str(p))
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_SECRET_KEY", "test-app-secret")
    return p


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(SqliteEventStore(":memory:")))


# ---- T4.3 TTS ----------------------------------------------------------------


def test_tts_disabled_reports_disabled(client, cfg_path):
    ConfigStore(cfg_path).sections.save_tts(TtsSettings(enabled=False))
    body = client.post("/api/tts/test").json()
    assert body["ok"] is False and body["status"] == "disabled"


def test_tts_openai_without_key_is_misconfigured(client, cfg_path):
    ConfigStore(cfg_path).sections.save_tts(
        TtsSettings(enabled=True, provider="openai", api_key_env="OPENAI_API_KEY")
    )
    body = client.post("/api/tts/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_tts_openai_happy_path_with_stubbed_synth(client, cfg_path, monkeypatch):
    store = ConfigStore(cfg_path)
    store.sections.save_tts(
        TtsSettings(enabled=True, provider="openai", api_key_env="openai", model="tts-1")
    )
    # store the key so resolution succeeds
    from disco.core.llm.secrets import SecretStore

    SecretStore().set_secret("openai", "sk-tts-live")

    async def fake_remote(text, voice, base_url, *, api_key="", model=""):
        assert api_key == "sk-tts-live" and text == "Disco"
        return [0.0] * 2400  # 0.1s of fake PCM @ 24 kHz — len()=2400

    import disco.tools.builtin.audio_overview as ao

    store.approvals.approve_origin("https://api.openai.com", "tts:openai", "openai")
    monkeypatch.setattr(ao, "_synthesize_remote", fake_remote)
    body = client.post("/api/tts/test").json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["byte_count"] == 2400 and body["provider"] == "openai"


# ---- T4.4 image generation ---------------------------------------------------


def test_image_remote_without_config_reports_misconfigured(client, cfg_path):
    """W-50: comfyui selected but no base URL → select_image_backend raises
    ImageGenNotConfigured; the probe SAYS `misconfigured` (no procedural fallback)."""
    ConfigStore(cfg_path).sections.save_image_gen(ImageGenSettings(provider="comfyui", base_url=""))
    body = client.post("/api/image-gen/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"
    assert body["procedural"] is False
    assert "configured" in body["detail"].lower()


# ---- T4.5 MCP ----------------------------------------------------------------


def test_mcp_no_target_is_misconfigured(client, cfg_path):
    body = client.post("/api/mcp/test", json={}).json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_mcp_stdio_without_command_is_misconfigured(client, cfg_path):
    body = client.post("/api/mcp/test", json={"transport": "stdio"}).json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_mcp_failed_handshake_is_not_a_500(client, cfg_path):
    """A handshake that fails (here: a stdio server whose command can't spawn)
    comes back ok=False at HTTP 200 with the real error — never a 500. This is
    the same classify-and-return path the streamable-HTTP transport uses on a
    connection refusal (the HTTP path can't be driven under the sync TestClient
    portal because the MCP SDK's anyio task group tears down in a foreign task —
    a harness artifact; the agent-server runs single-loop in production)."""
    event_store = SqliteEventStore(":memory:")
    approved_client = TestClient(create_app(event_store))
    cfg_store = ConfigStore(cfg_path)
    cfg = cfg_store.load()
    server = {
        "transport": "stdio",
        "command": ["/nonexistent/disco-probe-binary-xyz"],
        "url": "stdio://missing",
        "risk_tier": "medium",
        "enabled": True,
    }
    cfg_store.save(
        cfg.model_copy(
            update={
                "mcp": cfg.mcp.model_copy(update={"enabled": True, "servers": {"missing": server}})
            }
        )
    )
    from disco.tools.mcp.approval import compute_config_hash
    from disco.tools.mcp.migrations import create_mcp_config_approval

    create_mcp_config_approval(
        event_store._conn,
        "missing",
        compute_config_hash({"name": "missing", **server}),
    )
    r = approved_client.post("/api/mcp/test", json={"name": "missing"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["status"] in ("unreachable", "error")

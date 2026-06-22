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
    ConfigStore(cfg_path).save_tts(TtsSettings(enabled=False))
    body = client.post("/api/tts/test").json()
    assert body["ok"] is False and body["status"] == "disabled"


def test_tts_openai_without_key_is_misconfigured(client, cfg_path):
    ConfigStore(cfg_path).save_tts(
        TtsSettings(enabled=True, provider="openai", api_key_env="OPENAI_API_KEY")
    )
    body = client.post("/api/tts/test").json()
    assert body["ok"] is False and body["status"] == "misconfigured"


def test_tts_openai_happy_path_with_stubbed_synth(client, cfg_path, monkeypatch):
    ConfigStore(cfg_path).save_tts(
        TtsSettings(enabled=True, provider="openai", api_key_env="OPENAI_API_KEY", model="tts-1")
    )
    # store the key so resolution succeeds
    from disco.core.llm.secrets import SecretStore

    SecretStore().set_secret("OPENAI_API_KEY", "sk-tts-live")

    async def fake_remote(text, voice, base_url, *, api_key="", model=""):
        assert api_key == "sk-tts-live" and text == "Disco"
        return [0.0] * 2400  # 0.1s of fake PCM @ 24 kHz — len()=2400

    import disco.tools.builtin.audio_overview as ao

    monkeypatch.setattr(ao, "_synthesize_remote", fake_remote)
    body = client.post("/api/tts/test").json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["byte_count"] == 2400 and body["provider"] == "openai"


# ---- T4.4 image generation ---------------------------------------------------


def test_image_procedural_is_honest_ok(client, cfg_path):
    """Default procedural tier really produces bytes — ok, but flagged procedural
    so the chip never implies real diffusion ran."""
    ConfigStore(cfg_path).save_image_gen(ImageGenSettings(provider="procedural"))
    body = client.post("/api/image-gen/test").json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["procedural"] is True and body["byte_count"] > 0


def test_image_remote_without_config_reports_fallback(client, cfg_path):
    """comfyui selected but no base URL → select_image_backend falls back to
    procedural; the probe SAYS procedural-fallback (#76) instead of faking ok."""
    ConfigStore(cfg_path).save_image_gen(ImageGenSettings(provider="comfyui", base_url=""))
    body = client.post("/api/image-gen/test").json()
    assert body["ok"] is False and body["status"] == "procedural-fallback"
    assert body["procedural"] is True


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
    r = client.post(
        "/api/mcp/test",
        json={"transport": "stdio", "url": "/nonexistent/disco-probe-binary-xyz"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["status"] in ("unreachable", "error")

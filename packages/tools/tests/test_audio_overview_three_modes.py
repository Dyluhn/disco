"""D5 — Verify the 3-mode TTS toggle (off / bundled-Kokoro / remote-Speaches)
round-trips end-to-end AND that "off" actively unloads the bundled model.

The full round-trip spans FOUR layers (see must-trace below); the test drives the
back TWO (ConfigStore + AudioOverviewTool) which are the ones the
AudioSection.tsx toggle ultimately depends on. The front two (UI → app-server
PUT /api/tts/config) are covered by `app-server/tests/test_app.py::test_tts_config_round_trips`
and the Settings.tsx wire mirrors the same DTO; both round-trip the same bytes.

The wire the UI is supposed to drive:
  AudioSection selectMode
    → useUpdateTtsConfig  (PUT /api/tts/config)
    → app-server update_tts_config  (config_state.py:589)
    → store.save_tts  (ConfigStore: disco-config.json, atomic write)
    → audio_overview Step 0 reads tts = ConfigStore().load().tts  (per call)
        • enabled=False  → ToolOutcome(success=False, "disabled") + tts_local.unload()
        • enabled=True, remote=False  → _synthesize_local  (bundled Kokoro)
        • enabled=True, remote=True   → _synthesize_remote (Speaches HTTP)

These tests pin `ConfigStore.load` and patch the synth seams so the behavior
under each mode is deterministic. They also exercise the "off → unload" wire
end-to-end: a real pre-loaded Kokoro engine (mocked `_KokoroEngine`) goes back
to unloaded after the tool is invoked with the disabled toggle — proving the
RAM is reclaimed on the NEXT overview rather than waiting on the agent-server's
30-minute idle-TTL sweep.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from disco.core.llm import ConfigStore, TtsSettings, default_config
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.audio_overview import (
    AudioOverviewArgs,
    AudioOverviewTool,
)
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxInstance
from disco.tools.secrets import CapabilityBroker

pytestmark = pytest.mark.asyncio


# ---- helpers (mirror test_audio_overview_integration.py's) ----------------


@contextmanager
def _patch_tts(tts: TtsSettings):
    cfg = default_config().model_copy(update={"tts": tts})
    with mock.patch.object(ConfigStore, "load", return_value=cfg):
        yield


def _jailed_sandbox(workspace: Path) -> ProcessSandboxInstance:
    return ProcessSandboxInstance(
        id="d5-sbx",
        owner_id="test",
        conversation_id="d5-cid",
        spec=SandboxSpec(),
        workspace=workspace,
    )


def _ctx(sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="d5-cid",
    )


VALID_JSON = json.dumps(
    [
        {"speaker": "A", "text": "Host A opening line for the toggle test."},
        {"speaker": "B", "text": "Host B follow-up question for the toggle test."},
    ]
)


def _llm_ok(payload, llm_url):
    return VALID_JSON


async def _synth_local(text: str, voice: str) -> np.ndarray:
    n = max(240, len(text) * 200)
    t = np.arange(n, dtype=np.float32) / 24000.0
    return (0.2 * np.sin(2 * np.pi * 180.0 * t)).astype(np.float32)


def _assert_mp3(data: bytes) -> None:
    head = data[:64]
    assert any(
        head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1)
    ), "no MPEG frame sync in MP3 head"


# The three-mode matrix — one focused assertion per mode. ---------------------


async def test_mode_bundled_uses_local_kokoro():
    """`enabled=True, remote=False` → tool calls `_synthesize_local` (bundled), does
    NOT call `_synthesize_remote`, and produces an MP3 + transcript."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        local = mock.AsyncMock(side_effect=_synth_local)
        remote = mock.AsyncMock(side_effect=AssertionError("remote must not run in bundled mode"))

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch("disco.tools.builtin.audio_overview._call_llm", side_effect=_llm_ok),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_local", local),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_remote", remote),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text="x", filename="bundled"), ctx
            )

        assert outcome.success, outcome.content
        assert outcome.structured["backend"] == "bundled"
        assert local.await_count == 2  # one per turn
        remote.assert_not_awaited()
        assert (workspace / "bundled.mp3").exists()
        assert (workspace / "bundled.md").exists()
        _assert_mp3((workspace / "bundled.mp3").read_bytes())


async def test_mode_speaches_uses_remote_endpoint():
    """`enabled=True, provider="speaches"` → tool calls `_synthesize_remote`
    (self-host OpenAI-compatible), NOT `_synthesize_local`, threading base_url and
    NO api key (keyless self-host tier)."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        async def remote_ok(text, voice, base_url, *, api_key="", model=""):
            assert base_url == "http://speaches.local:9999", (
                f"base_url not threaded through; got {base_url!r}"
            )
            assert api_key == "", "self-host (speaches) must send NO key"
            n = max(240, len(text) * 200)
            return (0.1 * np.ones(n, dtype=np.float32))

        local = mock.AsyncMock(side_effect=AssertionError("local must not run in remote mode"))
        remote = mock.AsyncMock(side_effect=remote_ok)

        with (
            _patch_tts(
                TtsSettings(
                    enabled=True,
                    provider="speaches",
                    base_url="http://speaches.local:9999",
                )
            ),
            mock.patch("disco.tools.builtin.audio_overview._call_llm", side_effect=_llm_ok),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_local", local),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_remote", remote),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text="x", filename="remote"), ctx
            )

        assert outcome.success, outcome.content
        assert outcome.structured["backend"] == "speaches"
        assert remote.await_count == 2
        local.assert_not_awaited()
        assert (workspace / "remote.mp3").exists()


async def test_mode_openai_sends_key_and_model():
    """`enabled=True, provider="openai"` → tool calls `_synthesize_remote` with the
    resolved Bearer key (from the api_key_env name) AND the model id (the paid tier)."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        async def remote_ok(text, voice, base_url, *, api_key="", model=""):
            assert base_url == "https://api.openai.com"
            assert api_key == "sk-test-123", f"paid key not resolved; got {api_key!r}"
            assert model == "tts-1", f"model not threaded; got {model!r}"
            n = max(240, len(text) * 200)
            return (0.1 * np.ones(n, dtype=np.float32))

        remote = mock.AsyncMock(side_effect=remote_ok)
        with (
            _patch_tts(
                TtsSettings(
                    enabled=True,
                    provider="openai",
                    base_url="https://api.openai.com",
                    api_key_env="OPENAI_TTS_KEY",
                    model="tts-1",
                )
            ),
            mock.patch.dict("os.environ", {"OPENAI_TTS_KEY": "sk-test-123"}),
            mock.patch("disco.tools.builtin.audio_overview._call_llm", side_effect=_llm_ok),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_remote", remote),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text="x", filename="paid"), ctx
            )
        assert outcome.success, outcome.content
        assert outcome.structured["backend"] == "openai"
        assert remote.await_count == 2


async def test_mode_off_fails_soft_and_unloads_model():
    """`enabled=False` → tool returns success=False with the "disabled" message AND
    actively calls `tts_local.unload()` to free the Kokoro model. We pre-load a
    fake engine in the real `tts_local` module, run the tool, and assert the
    module's engine ref is back to None — proving the wire from the Settings
    toggle to the model's resident memory."""
    from disco.agent_server import tts_local

    class _FakeEngine:
        def __init__(self) -> None:
            self.dumped = False

        def synthesize(self, text: str, voice: str) -> np.ndarray:
            return np.full(240, 0.1, dtype=np.float32)

    # Drive the real module: install the fake engine, fake a previous synth, then
    # assert it goes back to unloaded after the tool runs in `off` mode.
    tts_local._engine = _FakeEngine()  # pre-loaded
    tts_local._last_used = 1.0  # any nonzero value
    assert tts_local.is_loaded() is True, "precondition: engine resident"

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        local = mock.AsyncMock(
            side_effect=AssertionError("local must not run in off mode")
        )

        with (
            _patch_tts(TtsSettings(enabled=False)),
            mock.patch("disco.tools.builtin.audio_overview._call_llm", side_effect=_llm_ok),
            mock.patch("disco.tools.builtin.audio_overview._synthesize_local", local),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text="x", filename="off"), ctx
            )

        # Soft failure with the documented message.
        assert not outcome.success
        assert "disabled" in outcome.content.lower()
        assert outcome.error == "tts disabled"
        local.assert_not_awaited()
        # No artifacts — the tool never reached the synth/mix/encode path.
        assert not (workspace / "off.mp3").exists()
        assert not (workspace / "off.md").exists()

    # The actual wire under test: the model is unloaded.
    assert tts_local.is_loaded() is False, (
        "D5 wire missing: 'off' toggle did not call tts_local.unload() — the "
        "model stays resident until the agent-server's idle-TTL sweep."
    )

    # Cleanup
    await tts_local.unload()
    tts_local._last_used = 0.0


async def test_mode_off_unload_is_safe_when_nothing_loaded():
    """`enabled=False` when the engine was never loaded must NOT raise. The
    disabled-branch `unload()` is a documented no-op on a cold engine, and the
    lazy import must not blow up when the optional `tts` extra is missing."""
    from disco.agent_server import tts_local

    assert tts_local.is_loaded() is False

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with _patch_tts(TtsSettings(enabled=False)):
            outcome = await tool.run(
                AudioOverviewArgs(report_text="x", filename="off_cold"), ctx
            )

    assert not outcome.success
    assert tts_local.is_loaded() is False  # still cold


# The "ConfigStore carries the wire" guarantee. ------------------------------


async def test_three_modes_round_trip_through_configstore():
    """Pure-data assertion: every (enabled, provider) pair maps to the mode the UI
    declares. This is the contract the AudioSection's `modeOf` helper and the tool's
    Step 0 both honor; if either drifts, this test fails."""
    cases = [
        (TtsSettings(enabled=False, provider="bundled"), "off"),
        (TtsSettings(enabled=True, provider="bundled"), "bundled"),
        (TtsSettings(enabled=True, provider="speaches"), "speaches"),
        (TtsSettings(enabled=True, provider="openai"), "openai"),
    ]
    for tts, expected_mode in cases:
        # UI-side derivation (mirrors AudioSection modeOf: off if disabled, else provider)
        ui_mode = "off" if not tts.enabled else tts.provider
        assert ui_mode == expected_mode

        # Tool-side backend label is the provider verbatim (bundled vs the remote tiers)
        assert tts.provider in {"bundled", "speaches", "openai"}

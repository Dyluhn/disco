"""Integration test: mock-TTS + mock-LLM end-to-end audio overview pipeline.

Drives the AudioOverviewTool over function-level mocks:
  report → LLM → JSON turn-script → per-turn PCM synth → mix in PCM → MP3 + transcript.

The TTS backend is selected from ConfigStore (Settings → Audio); these tests patch
`ConfigStore.load` so the toggle/backend/voices are deterministic rather than reading
the live disco-config.json. The synth seam is now PCM (float32 mono @ 24 kHz):
the bundled path is `_synthesize_local(text, voice)`, the remote path is
`_synthesize_remote(text, voice, url)`. Both return numpy arrays; the mixer encodes
the whole overview to MP3 once.

Tests:
  - Full happy path (bundled-local) with asserted artifact paths + file contents
  - Malformed-then-retry JSON path
  - Disabled toggle → clean failure, model never loads
  - Remote backend unreachable → clean failure (success=False + clear message)
  - Sandbox-backed file writes (jailed, not host fs)
  - Transcript matches script verbatim
  - LLM unreachable returns clean failure
"""

from __future__ import annotations

import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import httpx
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

# ---- mock data --------------------------------------------------------------

SAMPLE_REPORT = """# Q4 Performance Analysis

Revenue grew 15% year-over-year to $4.2B, driven by cloud services (+22%)
and enterprise subscriptions (+18%). Operating margin expanded 200bps to 28%.

Key risks: currency headwinds in EMEA (-3% impact), increased cloud infra costs
(+8% YoY), and competitive pressure in the SMB segment from two new entrants.

Outlook: FY2026 guidance raised to $17.5-18.0B (+12-15% YoY). Three strategic
initiatives: AI-powered analytics platform (Q1 launch), APAC expansion (H2),
and a new vertical SaaS offering for healthcare (pilot in Q2)."""

VALID_TURN_SCRIPT = [
    {
        "speaker": "A",
        "text": "Welcome to this analysis of Q4 performance. Revenue hit $4.2 billion, "
        "up fifteen percent year-over-year.",
    },
    {"speaker": "B", "text": "That's impressive growth. What drove it?"},
    {
        "speaker": "A",
        "text": "Cloud services led with twenty-two percent growth, "
        "followed by enterprise subscriptions at eighteen percent.",
    },
    {
        "speaker": "B",
        "text": "And the margin story is compelling too — "
        "two hundred basis points of expansion to twenty-eight percent.",
    },
    {
        "speaker": "A",
        "text": "Exactly. But there are headwinds. Currency in EMEA shaved three percent off, "
        "and cloud infra costs rose eight percent year-over-year.",
    },
    {"speaker": "B", "text": "What about the competitive landscape?"},
    {
        "speaker": "A",
        "text": "Two new entrants in the SMB segment are applying pressure, "
        "but the outlook remains strong.",
    },
    {
        "speaker": "B",
        "text": "They raised guidance for FY2026 to between $17.5 and $18 billion, "
        "a twelve to fifteen percent increase.",
    },
    {
        "speaker": "A",
        "text": "Three strategic bets: an AI analytics platform launching in Q1, "
        "APAC expansion in the second half, and a healthcare SaaS pilot in Q2.",
    },
    {"speaker": "B", "text": "That's a packed roadmap. Thanks for walking us through it."},
]

VALID_JSON_RESPONSE = json.dumps(VALID_TURN_SCRIPT)

MALFORMED_SCRIPT = "Sure! Here's the script: [Bad JSON without closing"


# ---- helpers ----------------------------------------------------------------


@contextmanager
def _patch_tts(tts: TtsSettings):
    """Pin ConfigStore.load to a config with the given TTS settings so the tool's
    Step 0 resolves deterministically (independent of the repo config file)."""
    cfg = default_config().model_copy(update={"tts": tts})
    with (
        mock.patch.object(ConfigStore, "load", return_value=cfg),
        mock.patch.object(ConfigStore, "origin_approved", return_value=True),
    ):
        yield


def _jailed_sandbox(workspace: Path) -> ProcessSandboxInstance:
    return ProcessSandboxInstance(
        id="test-sbx",
        owner_id="test",
        conversation_id="test-cid",
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
        conversation_id="test-cid",
    )


# ---- mocks for LLM + TTS calls ----------------------------------------------


def _mock_llm_happy(payload: dict, llm_url: str) -> str:
    return VALID_JSON_RESPONSE


def _mock_llm_malformed_first():
    """Returns malformed on first call, valid on second."""
    calls = [MALFORMED_SCRIPT, VALID_JSON_RESPONSE]
    idx = [0]

    def _inner(payload: dict, llm_url: str) -> str:
        result = calls[min(idx[0], len(calls) - 1)]
        idx[0] += 1
        return result

    return _inner


def _mock_llm_fail(payload: dict, llm_url: str) -> str:
    raise httpx.ConnectError("LLM offline")


async def _mock_synth_local(text: str, voice: str):
    """Bundled-Kokoro stand-in: float32 mono PCM whose length scales with the text
    (so the mixer assembles a non-trivial waveform). Recognizable low-amplitude tone."""
    n = max(240, len(text) * 200)  # >= a few ms even for short turns
    t = np.arange(n, dtype=np.float32) / 24000.0
    return (0.2 * np.sin(2 * np.pi * 180.0 * t)).astype(np.float32)


async def _mock_synth_remote_offline(text, voice, base_url, *, api_key="", model=""):
    raise httpx.ConnectError("Connection refused")


def _assert_mp3_head(data: bytes) -> None:
    """MP3 must carry an MPEG frame sync (0xFF Ex) within its head."""
    assert len(data) > 0
    head = data[:64]
    assert any(head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1)), (
        "no MPEG frame sync in MP3 head"
    )


# ---- tests ------------------------------------------------------------------


async def test_full_happy_path():
    """Report → LLM → valid JSON → bundled-local PCM synth → mixed MP3 + transcript."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="test_audio"),
                ctx,
            )

        # --- assertions ---
        assert outcome.success, f"Tool failed: {outcome.error}"
        assert outcome.artifacts == ["test_audio.mp3", "test_audio.md"]

        # Transcript file landed in workspace
        transcript_path = workspace / "test_audio.md"
        assert transcript_path.exists()
        transcript_text = transcript_path.read_text()
        assert "# Audio Overview Transcript" in transcript_text
        assert "Host A" in transcript_text
        assert "Host B" in transcript_text
        for turn in VALID_TURN_SCRIPT:
            assert turn["text"] in transcript_text

        # MP3 file landed in workspace, real encoded frames
        mp3_path = workspace / "test_audio.mp3"
        assert mp3_path.exists()
        _assert_mp3_head(mp3_path.read_bytes())

        # Structured output
        assert outcome.structured is not None
        assert outcome.structured["turn_count"] == len(VALID_TURN_SCRIPT)
        assert outcome.structured["voice_a"] == "af_heart"
        assert outcome.structured["voice_b"] == "af_bella"
        assert outcome.structured["backend"] == "bundled"


async def test_segmented_long_script_completes_transcript_and_audio_workflow():
    """A long indexed script reaches both artifacts with every turn exactly once."""

    calls: list[dict] = []

    def _segmented_llm(payload: dict, _llm_url: str):
        calls.append(payload)
        content = payload["messages"][-1]["content"]
        match = re.search(r"contiguous turn indexes (\d+) through (\d+)", content)
        assert match is not None
        start, end = (int(value) for value in match.groups())
        turns = [
            {
                "index": index,
                "speaker": "A" if index % 2 else "B",
                "text": f"Structured workflow turn {index}: " + "source detail " * 40,
            }
            for index in range(start, end + 1)
        ]
        from disco.tools.builtin.audio_overview import LLMResponse

        return LLMResponse(
            content=json.dumps({"total_turns": 12, "turns": turns}),
            finish_reason="stop",
        )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        tool = AudioOverviewTool()
        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_segmented_llm,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(
                    report_text=SAMPLE_REPORT + "\nCitation source: annual filing [1].",
                    filename="segmented_long",
                ),
                _ctx(_jailed_sandbox(workspace)),
            )

        assert outcome.success, outcome.content
        assert len(calls) == 3
        transcript = (workspace / "segmented_long.md").read_text()
        for index in range(1, 13):
            assert transcript.count(f"Structured workflow turn {index}:") == 1
        assert transcript.count("source detail") == 12 * 40
        _assert_mp3_head((workspace / "segmented_long.mp3").read_bytes())


async def test_repeated_provider_truncation_fails_before_tts_or_partial_files():
    calls = 0

    def _always_truncated(_payload: dict, _llm_url: str):
        nonlocal calls
        calls += 1
        from disco.tools.builtin.audio_overview import LLMResponse

        return LLMResponse(content="{", finish_reason="length")

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        synth = mock.AsyncMock(side_effect=_mock_synth_local)
        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_always_truncated,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                synth,
            ),
        ):
            outcome = await AudioOverviewTool().run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="truncated"),
                _ctx(_jailed_sandbox(workspace)),
            )

        assert not outcome.success
        assert calls == 3
        assert "finish_reason='length'" in outcome.content
        assert "no partial artifact" in outcome.content
        synth.assert_not_awaited()
        assert not (workspace / "truncated.mp3").exists()
        assert not (workspace / "truncated.md").exists()


async def test_malformed_then_retry():
    """First LLM response is malformed → validate fails → retry → valid script → success."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        llm_calls = []
        llm_mock = _mock_llm_malformed_first()

        def tracking_llm(payload, llm_url):
            llm_calls.append(payload)
            return llm_mock(payload, llm_url)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=tracking_llm,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="retry_test"),
                ctx,
            )

        assert outcome.success, f"Tool failed after retry: {outcome.content}"
        assert outcome.artifacts == ["retry_test.mp3", "retry_test.md"]

        # LLM was called TWICE: once (malformed) + retry
        assert len(llm_calls) == 2
        # Second call payload should have 3 messages (user + assistant + error feedback)
        second_call_msgs = llm_calls[1]["messages"]
        assert len(second_call_msgs) == 3
        assert "invalid" in second_call_msgs[2]["content"].lower()

        assert (workspace / "retry_test.mp3").exists()
        assert (workspace / "retry_test.md").exists()


async def test_disabled_toggle_fails_soft():
    """enabled=False → clean failure, and the model is NEVER loaded (no synth call)."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        synth = mock.AsyncMock(side_effect=_mock_synth_local)
        with (
            _patch_tts(TtsSettings(enabled=False)),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                synth,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="disabled_test"),
                ctx,
            )

        assert not outcome.success
        assert "disabled" in outcome.content.lower()
        synth.assert_not_awaited()  # the RAM gate held: no model load
        assert not (workspace / "disabled_test.mp3").exists()


async def test_remote_backend_offline():
    """Remote Speaches unreachable → clean failure, never crashes."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(
                TtsSettings(enabled=True, provider="speaches", base_url="http://localhost:8000")
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_remote",
                side_effect=_mock_synth_remote_offline,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="offline_test"),
                ctx,
            )

        assert not outcome.success, "Should fail when remote Speaches is offline"
        assert outcome.error is not None
        assert "unreachable" in outcome.content.lower() or "offline" in outcome.content.lower()
        assert not (workspace / "offline_test.mp3").exists()


async def test_sandbox_jailed_write():
    """Files are written THROUGH ctx.sandbox.write_file, not host filesystem."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="jail_test"),
                ctx,
            )

        assert outcome.success
        # Files are INSIDE the temp workspace, not in cwd
        cwd_mp3 = Path("jail_test.mp3")
        cwd_md = Path("jail_test.md")
        assert not cwd_mp3.exists(), "MP3 leaked to host cwd — must be sandbox-jailed"
        assert not cwd_md.exists(), "Transcript leaked to host cwd — must be sandbox-jailed"
        assert (workspace / "jail_test.mp3").exists()
        assert (workspace / "jail_test.md").exists()


async def test_transcript_matches_script():
    """The written transcript file contains ALL turn text verbatim."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="transcript_test"),
                ctx,
            )

        assert outcome.success
        transcript = (workspace / "transcript_test.md").read_text()
        for turn in VALID_TURN_SCRIPT:
            label = "Host A" if turn["speaker"] == "A" else "Host B"
            expected_line = f"**{label}:** {turn['text']}"
            assert expected_line in transcript, f"Missing turn: {expected_line}"


async def test_llm_failure_returned_cleanly():
    """If the LLM is unreachable, the tool returns a clean failure."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_fail,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="llm_fail"),
                ctx,
            )

        assert not outcome.success
        assert "LLM call failed" in outcome.content or "LLM" in outcome.content


async def test_artifact_list_includes_both_files():
    """ToolOutcome.artifacts lists exactly [mp3, transcript]."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            _patch_tts(TtsSettings(enabled=True, provider="bundled")),
            mock.patch(
                "disco.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "disco.tools.builtin.audio_overview._synthesize_local",
                side_effect=_mock_synth_local,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="my_audio"),
                ctx,
            )

        assert outcome.success
        assert outcome.artifacts == ["my_audio.mp3", "my_audio.md"]
        assert len(outcome.artifacts) == 2
        for art in outcome.artifacts:
            assert isinstance(art, str)
            assert art.endswith(".mp3") or art.endswith(".md")

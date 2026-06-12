"""Integration test: mock-Speaches + mock-LLM end-to-end audio overview pipeline.

Drives the AudioOverviewTool over function-level mocks:
  report → LLM → JSON turn-script → Speaches per-turn synth → mixed MP3 + transcript.
Tests:
  - Full happy path with asserted artifact paths + file contents
  - Malformed-then-retry JSON path
  - Offline Speaches → clean failure (success=False + clear message)
  - Sandbox-backed file writes (jailed, not host fs)
  - Transcript matches script verbatim
  - LLM unreachable returns clean failure
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import httpx
import pytest
from perpleximanus.tools.anatomy import ToolContext
from perpleximanus.tools.builtin.audio_overview import (
    AudioOverviewArgs,
    AudioOverviewTool,
    _call_llm,
    _synthesize,
)
from perpleximanus.tools.sandbox.base import SandboxSpec
from perpleximanus.tools.sandbox.process import ProcessSandboxInstance
from perpleximanus.tools.secrets import CapabilityBroker

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
    {"speaker": "A", "text": "Welcome to this analysis of Q4 performance. Revenue hit $4.2 billion, up fifteen percent year-over-year."},
    {"speaker": "B", "text": "That's impressive growth. What drove it?"},
    {"speaker": "A", "text": "Cloud services led with twenty-two percent growth, followed by enterprise subscriptions at eighteen percent."},
    {"speaker": "B", "text": "And the margin story is compelling too — two hundred basis points of expansion to twenty-eight percent."},
    {"speaker": "A", "text": "Exactly. But there are headwinds. Currency in EMEA shaved three percent off, and cloud infra costs rose eight percent year-over-year."},
    {"speaker": "B", "text": "What about the competitive landscape?"},
    {"speaker": "A", "text": "Two new entrants in the SMB segment are applying pressure, but the outlook remains strong."},
    {"speaker": "B", "text": "They raised guidance for FY2026 to between $17.5 and $18 billion, a twelve to fifteen percent increase."},
    {"speaker": "A", "text": "Three strategic bets: an AI analytics platform launching in Q1, APAC expansion in the second half, and a healthcare SaaS pilot in Q2."},
    {"speaker": "B", "text": "That's a packed roadmap. Thanks for walking us through it."},
]

VALID_JSON_RESPONSE = json.dumps(VALID_TURN_SCRIPT)

MALFORMED_SCRIPT = "Sure! Here's the script: [Bad JSON without closing"

# ---- mock MP3 frame (same as _audio_mixer._SILENCE_FRAME) -------------------

_MOCK_MP3_FRAME = (
    b"\xff\xfb\x90\x00"
    + b"\x00" * 413
)


def _make_speaches_response(text: str, voice: str) -> bytes:
    """Generate synthetic MP3 response. Varies length by text length."""
    n_frames = max(1, len(text) // 3)
    return _MOCK_MP3_FRAME * n_frames


# ---- helpers ----------------------------------------------------------------


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


# ---- mocks for LLM + Speaches calls -----------------------------------------


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


async def _mock_synthesize_happy(text: str, voice: str, speaches_url: str) -> bytes:
    return _make_speaches_response(text, voice)


async def _mock_synthesize_offline(text: str, voice: str, speaches_url: str) -> bytes:
    raise httpx.ConnectError("Connection refused")


# ---- tests ------------------------------------------------------------------


async def test_full_happy_path():
    """Report → LLM → valid JSON → Speaches per-turn → mixed MP3 + transcript."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_happy,
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

        # MP3 file landed in workspace
        mp3_path = workspace / "test_audio.mp3"
        assert mp3_path.exists()
        mp3_data = mp3_path.read_bytes()
        assert len(mp3_data) > 0
        assert mp3_data[0] == 0xFF
        assert (mp3_data[1] & 0xE0) == 0xE0

        # Structured output
        assert outcome.structured is not None
        assert outcome.structured["turn_count"] == len(VALID_TURN_SCRIPT)
        assert outcome.structured["voice_a"] == "af_heart"
        assert outcome.structured["voice_b"] == "af_bella"


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
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=tracking_llm,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_happy,
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


async def test_speaches_offline():
    """Speaches unreachable → clean failure, never crashes."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        sb = _jailed_sandbox(workspace)
        tool = AudioOverviewTool()
        ctx = _ctx(sb)

        with (
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_offline,
            ),
        ):
            outcome = await tool.run(
                AudioOverviewArgs(report_text=SAMPLE_REPORT, filename="offline_test"),
                ctx,
            )

        assert not outcome.success, "Should fail when Speaches is offline"
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
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_happy,
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
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_happy,
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

        with mock.patch(
            "perpleximanus.tools.builtin.audio_overview._call_llm",
            side_effect=_mock_llm_fail,
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
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._call_llm",
                side_effect=_mock_llm_happy,
            ),
            mock.patch(
                "perpleximanus.tools.builtin.audio_overview._synthesize",
                side_effect=_mock_synthesize_happy,
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

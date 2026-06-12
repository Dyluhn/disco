"""Unit tests for audio_overview — turn-script validation, retry, mixer
silence/chunk bounds, tool scoping/registration.

All Speaches and LLM calls are mocked — no live services hit.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import build_default_registry
from disco.tools.builtin._audio_mixer import (
    encode_mp3,
    mix_pcm,
    mix_turns_count,
)
from disco.tools.builtin.audio_overview import (
    _extract_json,
    _validate_turn_script,
)
from disco.tools.registry import agent_scope, research_scope
from disco.tools.secrets import CapabilityBroker


def _ctx(sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="test",
        conversation_id="test-cid",
    )


# ---- turn-script JSON extraction -------------------------------------------


def test_extract_json_clean_array():
    raw = '[{"speaker": "A", "text": "Hello"}, {"speaker": "B", "text": "Hi"}]'
    assert _extract_json(raw) == raw


def test_extract_json_with_surrounding_text():
    raw = 'Sure! Here is the script:\n```json\n[{"speaker": "A", "text": "Hello"}]\n```\nEnjoy!'
    result = _extract_json(raw)
    assert result.startswith("[")
    assert result.endswith("]")
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["speaker"] == "A"


def test_extract_json_markdown_code_block():
    raw = '```json\n[{"speaker": "A", "text": "Test"}]\n```'
    result = _extract_json(raw)
    parsed = json.loads(result)
    assert parsed[0]["speaker"] == "A"


def test_extract_json_no_code_block_fallback():
    raw = 'Some text [{"speaker": "B", "text": "Yes"}] more text'
    result = _extract_json(raw)
    parsed = json.loads(result)
    assert parsed[0]["speaker"] == "B"


# ---- turn-script validation ------------------------------------------------


def test_validate_valid_script():
    raw = json.dumps([
        {"speaker": "A", "text": "Welcome to the show."},
        {"speaker": "B", "text": "Thanks for having me."},
        {"speaker": "A", "text": "Let's dive into it."},
    ])
    turns, error = _validate_turn_script(raw)
    assert error is None
    assert len(turns) == 3
    assert turns[0].speaker == "A"
    assert turns[1].text == "Thanks for having me."


def test_validate_invalid_json():
    turns, error = _validate_turn_script("not json at all")
    assert turns is None
    assert "Invalid JSON" in error


def test_validate_not_array():
    turns, error = _validate_turn_script('{"speaker": "A"}')
    assert turns is None
    assert "array" in error.lower()


def test_validate_too_few_turns():
    turns, error = _validate_turn_script('[{"speaker": "A", "text": "Only one"}]')
    assert turns is None
    assert "2 turns" in error


def test_validate_bad_speaker():
    turns, error = _validate_turn_script(
        json.dumps([
            {"speaker": "A", "text": "Good"},
            {"speaker": "C", "text": "Bad speaker"},
        ])
    )
    assert turns is None
    assert "speaker" in error.lower()
    assert "C" in error


def test_validate_missing_text():
    turns, error = _validate_turn_script(
        json.dumps([
            {"speaker": "A", "text": "Good"},
            {"speaker": "B"},
        ])
    )
    assert turns is None
    assert "text" in error.lower()


def test_validate_empty_text():
    turns, error = _validate_turn_script(
        json.dumps([
            {"speaker": "A", "text": "Good"},
            {"speaker": "B", "text": "   "},
        ])
    )
    assert turns is None
    assert "non-empty" in error.lower() or "empty" in error.lower()


def test_validate_non_dict_turn():
    turns, error = _validate_turn_script(
        json.dumps([{"speaker": "A", "text": "Good"}, "not an object"])
    )
    assert turns is None
    assert "object" in error.lower()


# ---- mixer: PCM concatenation + MP3 encoding -------------------------------
# The mixer now works in float32 mono PCM: per-turn arrays are spliced with a
# silence gap of `sample_rate * silence_ms/1000` zeros, then the WHOLE overview
# is encoded to MP3 once. This is correct-by-construction at a single sample
# rate (the old MP3-frame splice glitched when silence was 44.1k and turns 24k).

_SR = 24000


def _tone(ms: int, freq: float = 220.0, sr: int = _SR) -> np.ndarray:
    """A recognizable mono float32 sine of `ms` milliseconds — stands in for a
    synthesized turn so we can assert on placement and length."""
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_mix_turns_count():
    assert mix_turns_count(0) == 0
    assert mix_turns_count(1) == 0
    assert mix_turns_count(2) == 1
    assert mix_turns_count(3) == 2
    assert mix_turns_count(5) == 4


def test_mix_pcm_empty():
    out = mix_pcm([], silence_ms=500, sample_rate=_SR)
    assert out.size == 0
    assert out.dtype == np.float32


def test_mix_pcm_single_no_gap():
    """One turn → exactly that turn, no leading/trailing silence."""
    t = _tone(100)
    out = mix_pcm([t], silence_ms=500, sample_rate=_SR)
    assert out.size == t.size
    assert np.allclose(out, t)


def test_mix_pcm_two_inserts_one_gap():
    """Two turns → t1 + (gap of sample_rate*silence_ms/1000 zeros) + t2."""
    t1 = _tone(100, freq=220.0)
    t2 = _tone(120, freq=330.0)
    silence_ms = 300
    gap = int(_SR * silence_ms / 1000)
    out = mix_pcm([t1, t2], silence_ms=silence_ms, sample_rate=_SR)
    assert out.size == t1.size + gap + t2.size
    # t1 at the head, t2 at the tail, zeros in the middle
    assert np.allclose(out[: t1.size], t1)
    assert np.allclose(out[-t2.size :], t2)
    assert np.all(out[t1.size : t1.size + gap] == 0.0)


def test_mix_pcm_three_inserts_two_gaps():
    t = _tone(80)
    silence_ms = 200
    gap = int(_SR * silence_ms / 1000)
    out = mix_pcm([t, t, t], silence_ms=silence_ms, sample_rate=_SR)
    assert out.size == 3 * t.size + 2 * gap


def test_mix_pcm_gap_scales_with_sample_rate():
    """Same silence_ms at double the rate → double the gap samples."""
    t = _tone(50, sr=_SR)
    g1 = mix_pcm([t, t], silence_ms=400, sample_rate=_SR).size - 2 * t.size
    g2 = mix_pcm([t, t], silence_ms=400, sample_rate=_SR * 2).size - 2 * t.size
    assert g2 == 2 * g1


def test_mix_pcm_skips_empty_turns():
    """Empty arrays are dropped so they don't create phantom gaps."""
    t = _tone(60)
    empty = np.zeros(0, dtype=np.float32)
    out = mix_pcm([empty, t, empty], silence_ms=300, sample_rate=_SR)
    assert out.size == t.size  # only the one real turn, no gaps


# ---- mixer: MP3 encoding ----------------------------------------------------


def test_encode_mp3_produces_valid_frames():
    """Encoding non-trivial PCM yields MP3 bytes that start with a frame sync."""
    pcm = _tone(500)
    mp3 = encode_mp3(pcm, sample_rate=_SR)
    assert isinstance(mp3, bytes)
    assert len(mp3) > 0
    # MPEG audio frame sync: 0xFF followed by top 3 bits set (0xE0). Some encoders
    # emit an ID3/info header first; scan a small prefix for the sync word.
    head = mp3[:64]
    assert any(
        head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1)
    ), "no MPEG frame sync found in MP3 head"


def test_encode_mp3_clips_out_of_range():
    """Out-of-[-1,1] PCM must not raise — it's clipped, not wrapped."""
    pcm = np.array([2.0, -2.0, 0.0, 1.5, -1.5] * 5000, dtype=np.float32)
    mp3 = encode_mp3(pcm, sample_rate=_SR)
    assert len(mp3) > 0


def test_encode_mp3_roundtrip_size_grows_with_audio():
    """A longer overview encodes to more bytes (sanity that PCM length matters)."""
    short = encode_mp3(_tone(200), sample_rate=_SR)
    long = encode_mp3(_tone(1000), sample_rate=_SR)
    assert len(long) > len(short)


# ---- tool registration + scoping -------------------------------------------


@pytest.mark.asyncio
async def test_tool_registered():
    reg = build_default_registry()
    assert "audio_overview" in reg.names()


@pytest.mark.asyncio
async def test_tool_in_agent_scope():
    reg = build_default_registry()
    agt_scope = agent_scope()
    assert "audio_overview" in agt_scope.allowed_tools
    tool = reg.get("audio_overview", scope=agt_scope)
    assert tool is not None
    assert tool.definition.name == "audio_overview"


@pytest.mark.asyncio
async def test_tool_excluded_from_research_scope():
    reg = build_default_registry()
    res_scope = research_scope()
    assert "audio_overview" not in res_scope.allowed_tools
    tool = reg.get("audio_overview", scope=res_scope)
    assert tool is None


# ---- tool definition shape -------------------------------------------------


@pytest.mark.asyncio
async def test_tool_definition_fields():
    reg = build_default_registry()
    tool = reg.get("audio_overview", scope=agent_scope())
    assert tool is not None
    d = tool.definition
    assert d.name == "audio_overview"
    assert "report_text" in d.args_model.model_fields
    assert d.runs_in == "sandbox"
    assert d.read_only is False

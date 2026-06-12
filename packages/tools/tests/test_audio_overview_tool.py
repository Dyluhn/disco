"""Unit tests for audio_overview — turn-script validation, retry, mixer
silence/chunk bounds, tool scoping/registration.

All Speaches and LLM calls are mocked — no live services hit.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeSandboxInstance
from perpleximanus.tools.anatomy import ToolContext
from perpleximanus.tools.builtin import build_default_registry
from perpleximanus.tools.builtin._audio_mixer import (
    generate_silence_mp3,
    mix_turns,
    mix_turns_count,
    mp3_duration_ms,
)
from perpleximanus.tools.builtin.audio_overview import (
    _extract_json,
    _validate_turn_script,
)
from perpleximanus.tools.registry import agent_scope, research_scope
from perpleximanus.tools.secrets import CapabilityBroker



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


# ---- mixer: silence generation + duration ----------------------------------


def test_generate_silence_mp3_500ms():
    """Silence MP3 of ~500ms should have roughly the right frame count."""
    data = generate_silence_mp3(500)
    duration = mp3_duration_ms(data)
    # Expect ~500ms, allow 100ms tolerance (frame granularity ~26ms)
    assert 400 <= duration <= 650, f"Expected ~500ms, got {duration:.0f}ms"
    assert len(data) > 0


def test_generate_silence_mp3_zero():
    assert generate_silence_mp3(0) == b""


def test_generate_silence_mp3_small():
    """Even 1ms should produce at least 1 frame."""
    data = generate_silence_mp3(1)
    duration = mp3_duration_ms(data)
    assert duration > 0
    assert len(data) >= 417  # at least one frame


def test_mp3_duration_known_silence():
    """Duration of a known number of frames should be accurate."""
    # 10 frames at 1152 samples/frame, 44100 Hz = 10 * 1152/44100 * 1000 ≈ 261.2ms
    data = _SILENCE_FRAME * 10
    duration = mp3_duration_ms(data)
    expected = (10 * 1152 / 44100) * 1000
    assert abs(duration - expected) < 5.0, f"Expected ~{expected:.1f}ms, got {duration:.1f}ms"


def test_mix_turns_count():
    assert mix_turns_count(0) == 0
    assert mix_turns_count(1) == 0
    assert mix_turns_count(2) == 1
    assert mix_turns_count(3) == 2
    assert mix_turns_count(5) == 4


def test_mix_turns_empty():
    assert mix_turns([]) == b""


def test_mix_turns_single():
    data = generate_silence_mp3(200)
    result = mix_turns([data])
    assert result == data


def test_mix_turns_two():
    """Two turns: one silence gap between them."""
    t1 = generate_silence_mp3(100)  # recognizable first chunk
    t2 = generate_silence_mp3(200)  # different length second chunk
    result = mix_turns([t1, t2], silence_ms=300)
    # Result should be longer than t1 + t2
    assert len(result) > len(t1) + len(t2)
    # t1 at the start
    assert result[: len(t1)] == t1
    # t2 at the end
    assert result[-len(t2) :] == t2


def test_mix_turns_three():
    """Three turns: two silence gaps."""
    t = generate_silence_mp3(100)
    result = mix_turns([t, t, t], silence_ms=300)
    # t at start, t at end
    assert result[: len(t)] == t
    assert result[-len(t) :] == t
    # Middle chunk has silence in it
    total_silence_inserted = len(result) - 3 * len(t)
    assert total_silence_inserted > 0


def test_mix_turns_silence_segment_explicit():
    """Using an explicit silence_segment parameter."""
    t1 = generate_silence_mp3(100)
    t2 = generate_silence_mp3(100)
    silence_seg = generate_silence_mp3(50)
    result = mix_turns([t1, t2], silence_ms=500, silence_segment=silence_seg)
    assert len(result) > len(t1) + len(t2)
    assert result[: len(t1)] == t1
    assert result[-len(t2) :] == t2


# ---- silcence frame size ---------------------------------------------------
# Use the mixer internals for frame size validation
from perpleximanus.tools.builtin._audio_mixer import _SILENCE_FRAME


def test_silence_frame_size():
    """Each silence frame should be 417 bytes (MPEG1, 128kbps, 44100Hz, no pad)."""
    assert len(_SILENCE_FRAME) == 417


def test_silence_frame_has_sync():
    """Frame must start with sync bytes 0xFF 0xFB (or 0xFF 0xFA)."""
    assert _SILENCE_FRAME[0] == 0xFF
    assert (_SILENCE_FRAME[1] & 0xE0) == 0xE0


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

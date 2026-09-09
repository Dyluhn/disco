"""The script stage must survive a driver that answers its own way.

Three of four live attempts on fresh VMs (agent-server on ollama.com /
deepseek-v4-flash, both modes) died in this stage on a report that was already
finished, every time because the pipeline demanded a shape the driver had never
promised. Each test below is one of those shapes: it FAILED before this pass
with a `turn_script` error, and produces a script now.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from disco.tools.builtin import audio_overview as audio

REPORT_TEXT = """Query: perovskite tandem cells

Executive summary: Efficiency passed thirty-three percent. Stability is the
open problem.

Section: Efficiency
Certified cells reached 33.9 percent in 2025. Two groups reproduced it.

Section: Stability
Damp-heat testing still fails at 1000 hours. Encapsulation is the suspect.
"""


def _turns(count: int, start: int = 1, prefix: str = "Turn") -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "speaker": "A" if index % 2 else "B",
            "text": f"{prefix} {index}: a spoken sentence about the findings.",
        }
        for index in range(start, start + count)
    ]


async def _run(call, *, mode: str = "podcast") -> Any:
    return await audio._generate_turn_script_result(
        REPORT_TEXT, "fast-driver", mode=mode, call_llm=call
    )


@pytest.mark.asyncio
async def test_whole_script_in_one_answer_is_accepted() -> None:
    """The driver ignores the window and writes the entire script at once."""
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        return audio.LLMResponse(
            content=json.dumps({"total_turns": 14, "turns": _turns(14)}),
            finish_reason="stop",
        )

    result = await _run(call)
    assert calls == 1
    assert result.source == "model"
    assert len(result.turns) == 14


@pytest.mark.asyncio
async def test_partial_answer_is_topped_up_not_failed() -> None:
    """The driver returns fewer turns than the window asked for; the rest are
    asked for instead of the run dying on "at least 4 contiguous turns"."""
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        # Two turns per answer, renumbered from 1 every time — the indexes are
        # wrong AND the batch is short. Both used to be fatal.
        return audio.LLMResponse(
            content=json.dumps(
                {"total_turns": 12, "turns": _turns(2, prefix=f"Answer {calls} turn")}
            ),
            finish_reason="stop",
        )

    result = await _run(call)
    assert len(result.turns) == 12
    assert result.source == "model"
    assert len({turn.text for turn in result.turns}) == 12


@pytest.mark.asyncio
async def test_extra_fields_and_missing_total_are_ignored() -> None:
    """No `total_turns`, no `index`, unknown keys, "Host A" speakers."""

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        return audio.LLMResponse(
            content=json.dumps(
                {
                    "title": "Perovskite tandems",
                    "estimated_minutes": 6,
                    "turns": [
                        {"speaker": "Host A", "text": "Welcome.", "emotion": "warm"},
                        {"speaker": "host_b", "text": "Glad to be here.", "id": "t2"},
                        {"speaker": "Host A", "text": "Efficiency passed thirty-three."},
                    ],
                }
            ),
            finish_reason="stop",
        )

    result = await _run(call)
    assert [turn.speaker for turn in result.turns[:3]] == ["A", "B", "A"]
    assert result.turns[0].text == "Welcome."


@pytest.mark.asyncio
async def test_json_wrapped_in_reasoning_and_prose_is_read() -> None:
    """A reasoning block and a sign-off around the JSON."""
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        body = json.dumps({"total_turns": 12, "turns": _turns(4, start=1 + (calls - 1) * 4)})
        return audio.LLMResponse(
            content=(
                "<think>The user wants a podcast. I will plan 12 turns.</think>\n"
                "Sure! Here's the batch:\n```json\n" + body + "\n```\n"
                "Let me know if you'd like a different tone."
            ),
            finish_reason="stop",
        )

    result = await _run(call)
    assert len(result.turns) == 12
    assert result.source == "model"


@pytest.mark.asyncio
async def test_truncated_answer_keeps_the_turns_that_arrived() -> None:
    """A cut-off array still decodes object by object, so a truncated answer
    contributes its complete turns instead of nothing."""
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        full = json.dumps({"total_turns": 12, "turns": _turns(4, start=1 + (calls - 1) * 2)})
        if calls == 1:
            # Chop mid-way through the third turn object.
            return audio.LLMResponse(
                content=full[: full.index('"index": 3')], finish_reason="length"
            )
        return audio.LLMResponse(content=full, finish_reason="stop")

    result = await _run(call)
    assert result.turns[0].text.startswith("Turn 1:")
    assert result.turns[1].text.startswith("Turn 2:")
    assert len(result.turns) == 12


@pytest.mark.asyncio
async def test_empty_answers_fall_back_to_the_report_script() -> None:
    """The driver returns an empty string every time. Bounded, then the report
    narrates itself — with the swap stated for the UI."""
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        return audio.LLMResponse(content="", finish_reason="stop")

    result = await _run(call)
    assert calls == 3  # bounded barren budget
    assert result.source == "fallback"
    assert "built straight from the report" in result.note
    assert result.turns[0].text == "Here's an overview of the research on perovskite tandem cells."
    assert any("Damp-heat testing" in turn.text for turn in result.turns)
    assert {turn.speaker for turn in result.turns} == {"A", "B"}  # still a dialogue


@pytest.mark.asyncio
async def test_single_mode_fallback_stays_one_voice() -> None:
    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        return audio.LLMResponse(content="", finish_reason="stop")

    result = await _run(call, mode="single")
    assert result.source == "fallback"
    assert {turn.speaker for turn in result.turns} == {"A"}


@pytest.mark.asyncio
async def test_no_model_output_and_no_report_says_exactly_that() -> None:
    """The ONLY remaining failure: nothing to narrate at all."""

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        return audio.LLMResponse(content="", finish_reason="stop")

    with pytest.raises(audio.AudioScriptGenerationError) as exc:
        await audio._generate_turn_script_result(
            "   ", "fast-driver", mode="podcast", call_llm=call
        )
    assert "no narration script and no report text" in str(exc.value)


@pytest.mark.asyncio
async def test_transport_failure_is_retried_once_then_degrades() -> None:
    calls = 0

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        nonlocal calls
        calls += 1
        raise TimeoutError("read timeout")

    result = await _run(call)
    assert calls == 2  # one retry, then stop asking
    assert result.source == "fallback"


@pytest.mark.asyncio
async def test_unrecognised_finish_reason_no_longer_discards_a_good_script() -> None:
    """A provider whose completion vocabulary we don't know used to kill a run
    that had already returned a complete, valid script."""

    async def call(_payload: dict[str, Any]) -> audio.LLMResponse:
        return audio.LLMResponse(
            content=json.dumps({"total_turns": 12, "turns": _turns(12)}),
            finish_reason="FINISH_REASON_STOP",
        )

    result = await _run(call)
    assert len(result.turns) == 12
    assert result.source == "model"


@pytest.mark.asyncio
async def test_script_call_is_not_sampled_like_prose() -> None:
    """Structured output at 0.7 is what let a fast driver wander off the shape."""
    payload = audio._build_segment_payload_for_model(
        REPORT_TEXT,
        "fast-driver",
        mode="podcast",
        start_turn=1,
        requested_turns=4,
        total_turns=None,
        prior_turns=(),
    )
    assert payload["temperature"] == 0.3


def test_long_script_is_summarised_not_cut_mid_turn() -> None:
    long_turns = [
        audio.Turn(
            speaker="A" if i % 2 else "B",
            text=" ".join(f"Sentence {i}-{s} carries about eight words here." for s in range(12)),
        )
        for i in range(1, 21)
    ]
    capped, was_capped = audio._cap_script_duration(long_turns, "podcast")

    assert was_capped
    spoken = sum(len(turn.text.split()) for turn in capped)
    assert spoken <= 9 * 155  # the ~9-minute budget
    assert all(turn.text.endswith(".") for turn in capped)  # never mid-sentence
    assert len(capped) >= 2


def test_a_short_script_is_left_alone() -> None:
    turns = [audio.Turn(speaker="A", text="Short and sweet."), audio.Turn(speaker="B", text="Yes.")]
    capped, was_capped = audio._cap_script_duration(turns, "podcast")
    assert not was_capped
    assert capped == turns

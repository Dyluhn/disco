"""A01: deterministic regression coverage for bounded audio-script assembly."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from disco.tools.builtin import audio_overview as audio


def _requested_range(payload: dict[str, Any]) -> tuple[int, int]:
    content = "\n".join(str(message.get("content", "")) for message in payload["messages"])
    match = re.search(r"contiguous turn indexes (\d+) through (\d+)", content)
    assert match is not None, content
    return int(match.group(1)), int(match.group(2))


def _batch_response(
    payload: dict[str, Any],
    *,
    total: int = 12,
    long_text: bool = False,
) -> audio.LLMResponse:
    start, end = _requested_range(payload)
    turns = []
    for index in range(start, end + 1):
        detail = (
            ("evidence-backed source detail " * 50).strip()
            if long_text
            else "evidence-backed source detail"
        )
        turns.append(
            {
                "index": index,
                "speaker": "A" if index % 2 else "B",
                "text": f"Turn {index}: {detail}.",
            }
        )
    return audio.LLMResponse(
        content=json.dumps({"total_turns": total, "turns": turns}),
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_http_adapter_preserves_length_finish_reason(monkeypatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {"content": '[{"speaker":"A","text":"cut'},
                        "finish_reason": "length",
                    }
                ]
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response()

    monkeypatch.setattr(audio.httpx, "AsyncClient", lambda **_kwargs: _Client())
    result = await audio._call_llm({"messages": []}, "http://provider.invalid/v1")
    assert result.finish_reason == "length"
    assert result.content.endswith('"cut')


@pytest.mark.asyncio
async def test_long_reasoning_script_is_assembled_from_complete_batches() -> None:
    payloads: list[dict[str, Any]] = []

    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        payloads.append(payload)
        return _batch_response(payload, long_text=True)

    turns = await audio._generate_segmented_turn_script(
        "CITATION-SOURCE-17 establishes the report fact.",
        "reasoning-model",
        mode="podcast",
        call_llm=call,
    )

    assert len(payloads) == 3
    assert len(turns) == 12
    assert [turn.text.split(":", 1)[0] for turn in turns] == [
        f"Turn {index}" for index in range(1, 13)
    ]
    assert all("CITATION-SOURCE-17" in payload["messages"][-1]["content"] for payload in payloads)
    assert sum(len(turn.text) for turn in turns) > 15_000


@pytest.mark.asyncio
async def test_length_stop_retries_smaller_batch_without_duplicate_turns() -> None:
    payloads: list[dict[str, Any]] = []

    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        payloads.append(payload)
        if len(payloads) == 1:
            return audio.LLMResponse(
                content='{"total_turns":12,"turns":[{"index":1,"text":"cut',
                finish_reason="length",
            )
        return _batch_response(payload)

    turns = await audio._generate_segmented_turn_script(
        "report", "reasoning-model", mode="podcast", call_llm=call
    )

    assert _requested_range(payloads[0]) == (1, 4)
    assert _requested_range(payloads[1]) == (1, 2)
    assert len(payloads) == 7  # one discarded truncation + six valid 2-turn batches
    assert [turn.text.split(":", 1)[0] for turn in turns] == [
        f"Turn {index}" for index in range(1, 13)
    ]
    assert "cut" not in " ".join(turn.text for turn in turns)


@pytest.mark.asyncio
async def test_segmented_single_mode_preserves_single_voice_contract() -> None:
    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        return _batch_response(payload, total=10)

    turns = await audio._generate_segmented_turn_script(
        "report", "reasoning-model", mode="single", call_llm=call
    )
    assert len(turns) == 10
    assert all(turn.speaker == "A" for turn in turns)


@pytest.mark.asyncio
async def test_repeated_length_stop_fails_actionably_and_bounded() -> None:
    payloads: list[dict[str, Any]] = []

    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        payloads.append(payload)
        return audio.LLMResponse(content="{", finish_reason="length")

    with pytest.raises(audio.AudioScriptGenerationError, match="finish_reason='length'") as exc:
        await audio._generate_segmented_turn_script(
            "report", "reasoning-model", mode="podcast", call_llm=call
        )

    assert len(payloads) == 3
    assert [_requested_range(payload) for payload in payloads] == [(1, 4), (1, 2), (1, 1)]
    assert "no partial artifact" in str(exc.value)


@pytest.mark.asyncio
async def test_duplicate_index_uses_one_normal_malformed_retry() -> None:
    payloads: list[dict[str, Any]] = []

    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        payloads.append(payload)
        if len(payloads) == 1:
            invalid = {
                "total_turns": 12,
                "turns": [
                    {"index": 1, "speaker": "A", "text": "Turn 1"},
                    {"index": 1, "speaker": "B", "text": "Duplicate"},
                    {"index": 3, "speaker": "A", "text": "Turn 3"},
                    {"index": 4, "speaker": "B", "text": "Turn 4"},
                ],
            }
            return audio.LLMResponse(content=json.dumps(invalid), finish_reason="stop")
        return _batch_response(payload)

    turns = await audio._generate_segmented_turn_script(
        "report", "reasoning-model", mode="podcast", call_llm=call
    )

    assert len(turns) == 12
    assert len(payloads[1]["messages"]) == 3
    assert "corrected" in payloads[1]["messages"][-1]["content"]
    assert len({turn.text for turn in turns}) == 12


@pytest.mark.asyncio
async def test_changed_total_is_rejected_after_one_bounded_correction() -> None:
    payloads: list[dict[str, Any]] = []

    async def call(payload: dict[str, Any]) -> audio.LLMResponse:
        payloads.append(payload)
        return _batch_response(payload, total=12 if len(payloads) == 1 else 13)

    with pytest.raises(audio.AudioScriptGenerationError, match="changed total_turns") as exc:
        await audio._generate_segmented_turn_script(
            "report", "reasoning-model", mode="podcast", call_llm=call
        )

    assert len(payloads) == 3
    assert _requested_range(payloads[0]) == (1, 4)
    assert _requested_range(payloads[1]) == (5, 8)
    assert len(payloads[2]["messages"]) == 3
    assert "no partial artifact" in str(exc.value)


@pytest.mark.asyncio
async def test_legacy_array_malformed_retry_remains_supported() -> None:
    calls = 0

    async def call(_payload: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json"
        return json.dumps(
            [
                {"speaker": "A", "text": "First complete turn."},
                {"speaker": "B", "text": "Second complete turn."},
            ]
        )

    turns = await audio._generate_segmented_turn_script(
        "report", "legacy-test-adapter", mode="podcast", call_llm=call
    )
    assert calls == 2
    assert [turn.text for turn in turns] == [
        "First complete turn.",
        "Second complete turn.",
    ]

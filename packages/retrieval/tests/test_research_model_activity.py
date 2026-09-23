"""`model_activity` — the run's only signal that a model is still producing.

Before this the backend went silent for the whole of a long model call and the
frontend, correctly, said "no signal". These tests hold the emitter to the two
things that make the signal worth rendering: every number came off a real
stream, and a call the contract does not name emits nothing at all.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm import (
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
)
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.stream_progress import StreamProgress
from disco.retrieval.deep_research import model_activity_events
from disco.retrieval.deep_research._progress_events import (
    RESEARCH_PROGRESS_ACTIONS,
    _ModelActivityHeartbeat,
    emit_model_activity,
    model_activity_stage,
)
from disco.retrieval.deep_research._writer_parts import review_call
from disco.retrieval.deep_research.writer import REVIEW_STAGES


def _emitter() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """The same `(kind, payload)` emitter the rest of the run's events use."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        seen.append((kind, payload))

    return seen, emit


def _sse(*chunks: dict[str, Any]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _router(content: bytes) -> DefaultLLMRouter:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    provider = OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))
    config = RouterConfig(
        models={"local": ModelEntry(model_id="m1", provider="fake", context_window=65_536)},
        default_model="local",
    )
    return DefaultLLMRouter(config, {"fake": provider})


def _progress(**overrides: Any) -> StreamProgress:
    fields: dict[str, Any] = {
        "stream_id": 1,
        "inspect_stage": "research_turn",
        "tokens_streamed": 10,
        "reasoning_tokens": 0,
        "seconds": 1.0,
        "first": True,
        "final": False,
    }
    fields.update(overrides)
    return StreamProgress(**fields)


# ---- which call a heartbeat speaks for --------------------------------------


@pytest.mark.parametrize(
    ("inspect_stage", "expected"),
    [
        ("research_turn", "research_turn"),
        ("report_draft", "draft"),
        ("report_draft_reask", "draft"),
        ("report_structure_reask", "draft"),
        ("report_rework", "rework"),
        ("report_rework_reask", "rework"),
        ("report_continuation", "continuation"),
        (*REVIEW_STAGES[:1], "review"),
        ("report_review_reask", "review"),
    ],
)
def test_every_declared_stage_the_run_records_maps_to_a_contract_stage(
    inspect_stage: str, expected: str
) -> None:
    """These are the exact labels the call sites already declare themselves by.

    The stage is READ off the call, never inferred from whatever the loop last
    emitted — an inferred stage would be the fabricated liveness the whole
    heartbeat exists to replace.
    """
    assert model_activity_stage(inspect_stage) == expected


def test_the_review_stages_the_writer_uses_are_all_covered() -> None:
    """Pinned to the writer's own constant, so a renamed stage fails here."""
    assert all(model_activity_stage(stage) == "review" for stage in REVIEW_STAGES)


@pytest.mark.parametrize("inspect_stage", [None, "", "router", "query_rewrite", "grounding"])
def test_a_call_the_contract_does_not_name_emits_nothing(inspect_stage: str | None) -> None:
    assert model_activity_stage(inspect_stage) is None


def test_the_action_is_declared_so_the_frontend_contract_suite_can_see_it() -> None:
    assert "model_activity" in RESEARCH_PROGRESS_ACTIONS


# ---- the payload ------------------------------------------------------------


async def test_the_payload_carries_exactly_what_the_heartbeat_renders() -> None:
    seen, emit = _emitter()

    await emit_model_activity(
        emit, stage="draft", tokens_streamed=1_840, seconds=42.37, call_ordinal=7
    )

    assert seen == [
        (
            "model_activity",
            {"stage": "draft", "tokens_streamed": 1_840, "seconds": 42.4, "call_ordinal": 7},
        )
    ]


async def test_reasoning_tokens_are_reported_only_when_the_provider_exposed_them() -> None:
    """On a model that thinks its ceiling away, 'producing nothing visible' and
    'producing nothing' are different states and must not look alike."""
    seen, emit = _emitter()

    await emit_model_activity(
        emit,
        stage="review",
        tokens_streamed=900,
        seconds=30.0,
        call_ordinal=2,
        reasoning_tokens=900,
    )
    await emit_model_activity(
        emit, stage="review", tokens_streamed=900, seconds=30.0, call_ordinal=3, reasoning_tokens=0
    )

    assert seen[0][1]["reasoning_tokens"] == 900
    assert "reasoning_tokens" not in seen[1][1]


# ---- the run-scoped ordinal -------------------------------------------------


async def test_each_stream_takes_the_next_ordinal_and_keeps_it_until_it_closes() -> None:
    seen, emit = _emitter()
    heartbeat = _ModelActivityHeartbeat(emit)

    await heartbeat(_progress(stream_id=11, first=True))
    await heartbeat(_progress(stream_id=11, first=False))
    await heartbeat(_progress(stream_id=11, first=False, final=True))
    await heartbeat(_progress(stream_id=12, first=True))

    assert [payload["call_ordinal"] for _, payload in seen] == [1, 1, 1, 2]


async def test_a_call_with_no_declared_stage_never_consumes_an_ordinal() -> None:
    """The rewriter and grounding calls ride the same router; they are not the
    run's model calls and must not renumber them."""
    seen, emit = _emitter()
    heartbeat = _ModelActivityHeartbeat(emit)

    await heartbeat(_progress(stream_id=1, inspect_stage="query_rewrite"))
    await heartbeat(_progress(stream_id=2, inspect_stage="report_draft"))

    assert [(payload["stage"], payload["call_ordinal"]) for _, payload in seen] == [("draft", 1)]


async def test_the_ordinal_map_stays_bounded_when_streams_die_without_closing() -> None:
    seen, emit = _emitter()
    heartbeat = _ModelActivityHeartbeat(emit)

    for stream_id in range(1, 400):
        await heartbeat(_progress(stream_id=stream_id, first=True))

    assert len(heartbeat._ordinals) <= 64
    # Numbering never restarts: the 399th call is still the 399th.
    assert seen[-1][1]["call_ordinal"] == 399


# ---- end to end through a real call site ------------------------------------


async def test_a_real_review_call_emits_its_heartbeat_through_the_run_emitter() -> None:
    """`review_call` is a deep-research call site, unchanged by this lane: it
    declares its stage in metadata and the seam does the rest."""
    content = _sse(
        {"choices": [{"delta": {"reasoning_content": "weighing the draft"}}]},
        {"choices": [{"delta": {"content": '{"passes": true}'}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    router = _router(content)
    seen, emit = _emitter()
    messages = [LLMMessage(role="user", content="review this")]
    metadata = {"conversation_id": "c1", "inspect_stage": "report_review"}

    with model_activity_events(emit):
        first = await review_call(router, messages, max_tokens=1_000, metadata=metadata)
        await review_call(router, messages, max_tokens=1_000, metadata=metadata)

    assert first.text == '{"passes": true}'
    assert {kind for kind, _ in seen} == {"model_activity"}
    stages = {payload["stage"] for _, payload in seen}
    assert stages == {"review"}
    # Two calls, two ordinals; the close report of each carries the real totals.
    finals = [payload for _, payload in seen if payload["tokens_streamed"] == 2]
    assert [payload["call_ordinal"] for payload in finals] == [1, 2]
    assert all(payload["seconds"] >= 0 for _, payload in seen)


async def test_concurrent_calls_inside_the_run_still_reach_the_same_emitter() -> None:
    """The scope is installed once around the whole run, so work the loop
    fans out into its own tasks reports too — and each still gets its own
    ordinal rather than sharing one."""
    content = _sse(
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    router = _router(content)
    seen, emit = _emitter()
    messages = [LLMMessage(role="user", content="review this")]
    metadata = {"conversation_id": "c1", "inspect_stage": "report_review"}

    with model_activity_events(emit):
        await asyncio.gather(
            *(review_call(router, messages, max_tokens=1_000, metadata=metadata) for _ in range(3))
        )

    assert {payload["call_ordinal"] for _, payload in seen} == {1, 2, 3}


async def test_a_stream_that_delivers_nothing_produces_no_heartbeat() -> None:
    """A ceiling hit with an empty content channel is silence, and stays silent
    — the UI's 'no signal for N s' is the honest account of that call."""
    content = _sse({"choices": [{"delta": {}, "finish_reason": "length"}]})
    router = _router(content)
    seen, emit = _emitter()

    with model_activity_events(emit):
        await review_call(
            router,
            [LLMMessage(role="user", content="review this")],
            max_tokens=1_000,
            metadata={"conversation_id": "c1", "inspect_stage": "report_review"},
        )

    assert len(seen) == 1
    assert seen[0][1]["state"] == "waiting"
    assert seen[0][1]["tokens_streamed"] == 0


async def test_outside_the_run_scope_nothing_is_emitted_at_all() -> None:
    content = _sse(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    router = _router(content)
    seen, emit = _emitter()

    with model_activity_events(emit):
        pass
    await review_call(
        router,
        [LLMMessage(role="user", content="review this")],
        max_tokens=1_000,
        metadata={"conversation_id": "c1", "inspect_stage": "report_review"},
    )

    assert seen == []


# ---- buffered transports ----------------------------------------------------


def _buffered_router() -> DefaultLLMRouter:
    """A driver whose transport has no chunks at all.

    The provider decides stream vs buffered from the ENDPOINT
    (`_responses_api.is_responses_endpoint`), never from a model name, so this
    is that decision made the way the product makes it.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "m1",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "the whole answer at once"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 34},
            },
        )

    provider = OpenAIProvider(
        "http://fake/v1/responses", name="fake", transport=httpx.MockTransport(handler)
    )
    config = RouterConfig(
        models={"local": ModelEntry(model_id="m1", provider="fake", context_window=65_536)},
        default_model="local",
    )
    return DefaultLLMRouter(config, {"fake": provider})


async def test_a_buffered_driver_reports_once_at_call_start_instead_of_going_silent() -> None:
    """A Responses-API driver delivers everything in one POST.

    Nothing is observed for the whole call, so the run used to read "no signal
    for N s" — true of the transport, and read by everyone as a stalled model.
    The call start IS observable, so it is reported: stage, zero delivered, and
    `streams: False` saying no further report is coming.
    """
    seen, emit = _emitter()

    with model_activity_events(emit):
        await review_call(
            _buffered_router(),
            [LLMMessage(role="user", content="review this")],
            max_tokens=1_000,
            metadata={"conversation_id": "c1", "inspect_stage": "report_review"},
        )

    assert len(seen) == 1
    kind, payload = seen[0]
    assert kind == "model_activity"
    assert payload["stage"] == "review"
    assert payload["tokens_streamed"] == 0
    assert payload["streams"] is False
    assert payload["call_ordinal"] == 1


async def test_a_streaming_driver_never_carries_the_buffered_marker() -> None:
    """`streams` is present only when it is False, so the field's presence is
    itself the fact — a streamed heartbeat says nothing about the transport."""
    content = _sse(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    seen, emit = _emitter()

    with model_activity_events(emit):
        await review_call(
            _router(content),
            [LLMMessage(role="user", content="review this")],
            max_tokens=1_000,
            metadata={"conversation_id": "c1", "inspect_stage": "report_review"},
        )

    assert seen
    assert all("streams" not in payload for _, payload in seen)

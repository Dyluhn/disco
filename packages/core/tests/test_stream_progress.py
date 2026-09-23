"""The provider-stream progress seam — what the host can honestly say is happening.

The rule these tests hold the code to: a report is only ever produced by a
chunk that actually arrived. There is no timer, so a quiet stream is silent
here and the silence is what the caller renders.
"""

from __future__ import annotations

import json

import httpx
import pytest
from disco.core.events import LLMMessage
from disco.core.llm.openai_provider import OpenAIProvider
from disco.core.llm.stream_progress import (
    PROGRESS_INTERVAL_S,
    ProgressObserver,
    StreamProgress,
    StreamProgressReporter,
    observe_stream_progress,
    reporter_for,
)
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
)


class _Clock:
    """A hand-driven monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _collector() -> tuple[list[StreamProgress], ProgressObserver]:
    seen: list[StreamProgress] = []

    async def observe(progress: StreamProgress) -> None:
        seen.append(progress)

    return seen, observe


def _reporter(
    clock: _Clock, seen_into: list[StreamProgress], *, stage: str | None = None
) -> StreamProgressReporter:
    async def observe(progress: StreamProgress) -> None:
        seen_into.append(progress)

    return StreamProgressReporter(observe, inspect_stage=stage, clock=clock)


# ---- the throttle -----------------------------------------------------------


async def test_a_thousand_chunks_over_twelve_seconds_report_a_handful_of_times():
    """The heartbeat is throttled, not per token — and it still closes once."""
    clock = _Clock()
    seen: list[StreamProgress] = []
    reporter = _reporter(clock, seen)

    total_s = 12.0
    chunks = 1_000
    for i in range(1, chunks + 1):
        clock.now = total_s * i / chunks
        await reporter.observed(1, 0)
    await reporter.close()

    progress = [p for p in seen if not p.final]
    closes = [p for p in seen if p.final]
    assert len(progress) <= 3, [p.seconds for p in progress]
    assert len(closes) == 1
    # Nothing is ever re-counted or lost: each report carries the running total.
    assert [p.tokens_streamed for p in seen] == sorted(p.tokens_streamed for p in seen)
    assert closes[0].tokens_streamed == chunks
    # Reports are spaced by at least the interval; the first is the
    # prefill→decode transition and is not held back for it.
    assert progress[0].first is True and progress[0].seconds == pytest.approx(total_s / chunks)
    gaps = [b.seconds - a.seconds for a, b in zip(progress, progress[1:], strict=False)]
    assert all(gap >= PROGRESS_INTERVAL_S for gap in gaps), gaps


async def test_seconds_are_measured_from_the_call_not_from_the_first_chunk():
    """A four-minute prefill is four minutes the caller waited, and says so."""
    clock = _Clock()
    seen: list[StreamProgress] = []
    reporter = _reporter(clock, seen)

    clock.now = 240.0
    await reporter.observed(1, 0)

    assert seen[0].seconds == pytest.approx(240.0)


async def test_a_stream_that_delivered_nothing_reports_nothing():
    """No chunk, no claim. The caller's own 'quiet for N s' is the truth here."""
    clock = _Clock()
    seen: list[StreamProgress] = []
    reporter = _reporter(clock, seen)

    clock.now = 600.0
    await reporter.observed(0, 0)
    await reporter.close()

    assert seen == []


async def test_reasoning_deltas_count_and_are_reported_separately():
    clock = _Clock()
    seen: list[StreamProgress] = []
    reporter = _reporter(clock, seen)

    await reporter.observed(0, 3)
    clock.now = PROGRESS_INTERVAL_S + 1
    await reporter.observed(2, 1)
    await reporter.close()

    assert [(p.tokens_streamed, p.reasoning_tokens) for p in seen] == [(3, 3), (6, 4), (6, 4)]


async def test_close_is_reported_once_however_often_it_is_called():
    clock = _Clock()
    seen: list[StreamProgress] = []
    reporter = _reporter(clock, seen)

    await reporter.observed(1, 0)
    await reporter.close()
    await reporter.close()

    assert [p.final for p in seen] == [False, True]


async def test_an_observer_that_raises_never_breaks_the_stream():
    async def explode(progress: StreamProgress) -> None:
        raise RuntimeError("observer is broken")

    reporter = StreamProgressReporter(explode, clock=_Clock())
    await reporter.observed(1, 0)
    await reporter.close()  # no exception escapes


async def test_no_observer_installed_means_no_reporter_at_all():
    assert reporter_for({"inspect_stage": "report_review"}) is None


async def test_the_installed_observer_is_scoped_to_its_block():
    seen, observe = _collector()
    with observe_stream_progress(observe):
        assert reporter_for(None) is not None
    assert reporter_for(None) is None
    assert seen == []


# ---- through the real adapter -----------------------------------------------


def _req(stage: str | None = "report_review") -> CompletionRequest:
    metadata = {"conversation_id": "c1", "inspect_stage": stage} if stage else None
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="hi")],
        max_tokens=50,
        metadata=metadata,
    )


def _sse(*chunks: dict) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _stream_provider(content: bytes) -> OpenAIProvider:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    return OpenAIProvider("http://fake/v1", name="fake", transport=httpx.MockTransport(handler))


async def test_a_buffered_complete_still_reports_because_it_streams_underneath():
    """`complete` is what deep research calls; it rides the streamed transport."""
    content = _sse(
        {"choices": [{"delta": {"reasoning_content": "weighing it"}}]},
        {"choices": [{"delta": {"content": "the "}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    seen, observe = _collector()

    with observe_stream_progress(observe):
        response = await _stream_provider(content).complete(_req(), model="m1")

    assert response.text == "the answer"
    assert [p.first for p in seen] == [True, False, False]
    assert [p.final for p in seen] == [False, False, True]
    assert seen[0].state == "waiting" and seen[0].tokens_streamed == 0
    assert seen[-1].tokens_streamed == 3  # two content deltas + one reasoning
    assert seen[-1].reasoning_tokens == 1
    assert seen[-1].inspect_stage == "report_review"
    # One stream, so one identity — the caller's ordinal never splits a call.
    assert len({p.stream_id for p in seen}) == 1


async def test_a_stream_that_only_carries_a_finish_reason_reports_nothing():
    """The exact shape of L25's failure: a ceiling hit with nothing delivered."""
    content = _sse({"choices": [{"delta": {}, "finish_reason": "length"}]})
    seen, observe = _collector()

    with observe_stream_progress(observe):
        await _stream_provider(content).complete(_req(), model="m1")

    assert len(seen) == 1
    assert seen[0].state == "waiting" and seen[0].tokens_streamed == 0


async def test_two_calls_open_two_streams_with_distinct_identities():
    content = _sse(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    seen, observe = _collector()
    provider = _stream_provider(content)

    with observe_stream_progress(observe):
        await provider.complete(_req(), model="m1")
        await provider.complete(_req(), model="m1")

    assert len({p.stream_id for p in seen}) == 2

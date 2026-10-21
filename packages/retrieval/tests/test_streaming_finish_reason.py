"""Only complete streamed answers and complete optional questions reach the UI."""

import asyncio

import pytest
from disco.core.llm import CompletionResponse, ModelRole, StreamChunk, TokenUsage
from disco.retrieval import LexicalReranker
from disco.retrieval.streaming import _follow_ups, stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit
from test_search_context_budget import _wire_envelope

URL = "https://example.test/source"
GOOD = "The measured output is twelve units. [[source_p0]]"
CUT = "This discarded draft looks cited but is incomplete. [[source_p0]]"
QUESTIONS = "Which evidence supports the measurement?\nWhat limitations affect the result?"


def response(text, reason="stop"):
    return CompletionResponse(
        text=text, finish_reason=reason, usage=TokenUsage(input_tokens=1, output_tokens=1),
        model_used="arbitrary-compatible-model",
    )


class Router:
    def __init__(self, answers=(), followups=()):
        self.answers = list(answers)
        self.followups = list(followups) or [response(QUESTIONS)]
        self.stream_requests = []
        self.complete_requests = []

    def context_token_limit(self, role):
        return 4096 if role == ModelRole.RAG_ANSWERER else 2048

    def output_token_limit(self, role):
        return 384 if role == ModelRole.RAG_ANSWERER else 128

    def check(self, request):
        assert _wire_envelope(request) <= self.context_token_limit(request.profile.role)
        assert request.max_tokens <= self.output_token_limit(request.profile.role)

    async def stream_complete(self, request):
        self.check(request)
        self.stream_requests.append(request)
        text, reason = self.answers.pop(0)
        yield StreamChunk(delta_text=text[:12])
        yield StreamChunk(delta_text=text[12:])
        yield StreamChunk(done=True, final=response(text, reason))

    async def complete(self, request):
        self.check(request)
        self.complete_requests.append(request)
        return self.followups.pop(0)


async def collect(router):
    return [frame async for frame in stream_research_answer(
        "What was measured?", router=router, search=FakeSearchProvider([hit(URL)]),
        extraction=FakeExtractionProvider({URL: "The measured output is twelve units. " * 500}),
        reranker=LexicalReranker(),
        nli=FakeNLI({
            "The measured output is twelve units": "entail",
            "This discarded draft looks cited but is incomplete": "entail",
        }),
    )]


async def test_length_terminal_discards_draft_then_repacks_one_complete_answer():
    router = Router([(CUT, "length"), (GOOD, "stop")])
    frames = await collect(router)
    assert len(router.stream_requests) == 2
    assert [req.attempt for req in router.stream_requests] == [1, 2]
    assert router.stream_requests[1].enable_thinking is False
    assert "shorter" in router.stream_requests[1].messages[-1].content
    assert CUT not in str(router.stream_requests[1].messages)
    assert "".join(frame.get("token", "") for frame in frames) == GOOD
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert final["passages"][0]["text"] in router.stream_requests[1].messages[1].content
    assert "discarded" not in str(final)
    assert frames[-1] == {"type": "state", "status": "finished"}


async def test_two_length_terminations_fail_without_publishing_even_cited_complete_sentences():
    router = Router([(GOOD, "length"), (GOOD, "length")])
    frames = await collect(router)
    assert len(router.stream_requests) == 2 and router.complete_requests == []
    assert not [frame for frame in frames if frame["type"] in {"token", "final"}]
    assert frames[-1]["type"] == "error" and "output limit" in frames[-1]["message"]


@pytest.mark.parametrize("reason", ["error", "content_filter", "tool_calls"])
async def test_non_answer_terminal_status_does_not_certify_partial_text(reason):
    router = Router([(GOOD, reason)])
    frames = await collect(router)
    assert len(router.stream_requests) == 1 and router.complete_requests == []
    assert not [frame for frame in frames if frame["type"] in {"token", "final"}]
    assert frames[-1]["type"] == "error" and reason in frames[-1]["message"]


async def test_complete_stream_keeps_existing_token_frames_and_does_not_repair():
    router = Router([(GOOD, "stop")])
    frames = await collect(router)
    tokens = [frame["token"] for frame in frames if frame["type"] == "token"]
    assert len(tokens) > 1 and "".join(tokens) == GOOD
    assert len(router.stream_requests) == 1
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert final["follow_ups"] == QUESTIONS.splitlines()


async def test_cancellation_does_not_repair_or_publish_buffered_text():
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Paused(Router):
        async def stream_complete(self, request):
            self.stream_requests.append(request)
            try:
                yield StreamChunk(delta_text=CUT)
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    router = Paused()
    task = asyncio.create_task(collect(router))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert len(router.stream_requests) == 1 and router.complete_requests == []


async def test_length_followups_get_one_bounded_repair_without_partial_question():
    router = Router(followups=[
        response(QUESTIONS + "\nCan the IANA example domains", "length"),
        response("Which source is authoritative?\nWhat examples are reserved?"),
    ])
    result = await _follow_ups(router, "Example domains?", "Known answer. " * 1000)
    assert result == ["Which source is authoritative?", "What examples are reserved?"]
    assert [req.attempt for req in router.complete_requests] == [1, 2]


async def test_stop_followups_drop_incomplete_optional_line_without_losing_complete_questions():
    router = Router(followups=[response(QUESTIONS + "\nCan the IANA example domains")])
    assert await _follow_ups(router, "Example domains?", "Known answer.") == QUESTIONS.splitlines()
    assert len(router.complete_requests) == 1


@pytest.mark.parametrize("reason,calls", [("length", 2), ("error", 1), ("content_filter", 1)])
async def test_unusable_followups_fall_back_without_failing_completed_answer(reason, calls):
    router = Router(
        [(GOOD, "stop")], followups=[response(QUESTIONS + "\nTruncated", reason)] * calls,
    )
    frames = await collect(router)
    assert len(router.complete_requests) == calls
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert len(final["follow_ups"]) == 3
    assert all(question.endswith("?") for question in final["follow_ups"])
    assert "Truncated" not in str(final)
    assert frames[-1] == {"type": "state", "status": "finished"}

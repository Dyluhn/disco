"""Real Standard Search envelopes fit declared role capacities before dispatch."""

from __future__ import annotations

import pytest
from disco.core.llm import (
    DefaultLLMRouter,
    LLMContextWindowExceeded,
    ModelEntry,
    ModelRole,
    RouterConfig,
)
from disco.core.llm.types import StreamChunk
from disco.retrieval._answer_budget import answer_request, followup_request
from disco.retrieval.models import Passage
from disco.retrieval.streaming import _answer_prompt, _generate_visible_answer


def _router(context=4096, output=512, identity="arbitrary-instruction-model"):
    return DefaultLLMRouter(
        RouterConfig(
            models={
                "answer": ModelEntry(
                    model_id=identity,
                    provider="arbitrary-endpoint",
                    context_window=context,
                    max_output_tokens=output,
                )
            },
            default_model="answer",
        ),
        {},
    )


def _passage(text, id="evidence0"):
    return Passage(
        id=id,
        source_url="https://example.test/source",
        source_title="Source",
        text=text,
        char_start=100,
        char_end=100 + len(text),
    )


def _wire_envelope(request):
    # Independent conservative capacity assertion including message framing.
    return (
        sum(len(m.content.encode("utf-8")) + 64 for m in request.messages)
        + 128
        + request.max_tokens
    )


@pytest.mark.parametrize("think", [False, True])
@pytest.mark.parametrize("attempt", [1, 2])
def test_long_ranked_evidence_is_packed_with_output_reserved(think, attempt):
    evidence = [_passage("Paris is the capital of France. " * 400, f"source{i}") for i in range(8)]
    originals = [p.model_dump() for p in evidence]
    question = "What is the capital of France? One short sentence and a source."
    request, selected = answer_request(
        _router(), evidence, lambda ps: _answer_prompt(question, ps), think=think, attempt=attempt
    )
    assert _wire_envelope(request) <= 4096
    assert request.max_tokens == 512
    assert selected and len(selected) < len(evidence)
    assert selected[0].id == "source0"
    assert selected[0].source_url == evidence[0].source_url
    assert evidence[0].text.startswith(selected[0].text)
    assert selected[0].char_end == 100 + len(selected[0].text)
    assert question in request.messages[-1].content
    assert [p.model_dump() for p in evidence] == originals


def test_utf8_excerpt_keeps_valid_text_and_only_real_source_ids():
    source = _passage("城市🌍引用正文。" * 900)
    request, selected = answer_request(
        _router(), [source], lambda ps: _answer_prompt("Which city?", ps), think=False, attempt=1
    )
    assert _wire_envelope(request) <= 4096
    assert selected[0].text and source.text.startswith(selected[0].text)
    assert "�" not in selected[0].text
    assert "[[evidence0]]" in request.messages[-1].content


def test_small_evidence_remains_byte_identical_and_unknown_router_keeps_protocol_compatibility():
    passages = [_passage("Paris is the capital of France.")]

    def render(ps):
        return _answer_prompt("Which city?", ps)

    for router in (_router(), object()):
        request, selected = answer_request(router, passages, render, think=False, attempt=1)
        assert request.messages == render(passages)
        assert selected == passages


@pytest.mark.parametrize("identity", ["opaque-model", "gpt-4o", "qwen3"])
def test_capacity_queries_follow_assigned_role_not_model_names(identity):
    config = RouterConfig(
        models={
            "large": ModelEntry(
                model_id=identity, provider="one", context_window=32000, max_output_tokens=5000
            ),
            "small": ModelEntry(
                model_id="other", provider="two", context_window=4096, max_output_tokens=512
            ),
        },
        default_model="large",
        assignments={ModelRole.RAG_ANSWERER: "small"},
    )
    router = DefaultLLMRouter(config, {})
    assert router.context_token_limit(ModelRole.RAG_ANSWERER) == 4096
    assert router.output_token_limit(ModelRole.RAG_ANSWERER) == 512
    assert router.context_token_limit(ModelRole.SUMMARIZER) == 32000
    assert router.output_token_limit(ModelRole.SUMMARIZER) == 5000


class RecordingRouter:
    def __init__(self, context=4096):
        self.context = context
        self.requests = []

    def context_token_limit(self, role):
        return self.context

    def output_token_limit(self, role):
        return 512

    async def complete(self, request):
        raise AssertionError("this test only streams")

    async def stream_complete(self, request):
        self.requests.append(request)
        assert _wire_envelope(request) <= self.context
        if len(self.requests) == 2:
            yield StreamChunk(delta_text="Paris is the capital. [[evidence0]]")
        yield StreamChunk(done=True)


async def test_empty_answer_repair_repacks_reminder_before_dispatch():
    router = RecordingRouter()
    source = _passage("Paris is the capital. " * 1000)
    text, frames, selected = await _generate_visible_answer(
        router, "Which city?", [source], think=True
    )
    assert text == "Paris is the capital. [[evidence0]]"
    assert len(router.requests) == 2
    assert [r.max_tokens for r in router.requests] == [512, 512]
    assert len(router.requests[1].messages) == 3
    assert selected[0].text in router.requests[1].messages[1].content
    assert source.text.startswith(selected[0].text)
    assert "".join(f["token"] for f in frames) == text


async def test_impossible_fixed_question_fails_locally_without_provider_probe():
    router = RecordingRouter(context=500)
    with pytest.raises(LLMContextWindowExceeded, match="No inference was sent"):
        await _generate_visible_answer(
            router, "Keep this exact question. " * 100, [_passage("Evidence")], think=False
        )
    assert not router.requests


@pytest.mark.parametrize("attempt", [1, 2])
def test_followup_reserves_its_own_role_output_and_packs_unicode_answer(attempt):
    request = followup_request(_router(), "Which city?", "城市🌍" * 1000, attempt)
    assert _wire_envelope(request) <= 4096
    assert request.max_tokens <= 512
    assert "Which city?" in request.messages[-1].content
    assert request.messages[0].role == "system"


async def test_round_verifies_and_returns_only_evidence_sent_to_answerer():
    from disco.retrieval.streaming import _run_research_round
    from research_fakes import FakeExtractionProvider, FakeSearchProvider, hit

    class Router(RecordingRouter):
        async def stream_complete(self, request):
            self.requests.append(request)
            yield StreamChunk(
                delta_text="Paris is the capital. [[first_p0]] Invented claim. [[second_p0]]"
            )
            yield StreamChunk(done=True)

    class Ranked:
        async def rerank(self, query, passages, *, top_k):
            return sorted(passages, key=lambda p: p.id)[:top_k]

    class Verifier:
        def __init__(self):
            self.premises = []

        def entail(self, premise, hypothesis):
            return "entail"

        def score(self, premise, hypothesis):
            self.premises.append(premise)
            return 1.0

    first = "https://example.test/first"
    second = "https://example.test/second"
    original = "Paris is the capital of France. " * 1000
    router = Router()
    nli = Verifier()
    result = await _run_research_round(
        "Which city?",
        search=FakeSearchProvider([hit(first), hit(second)]),
        extraction=FakeExtractionProvider({first: original, second: "Never sent evidence."}),
        reranker=Ranked(),
        router=router,
        nli=nli,
        think=False,
        domains_deny=frozenset(),
        discover_limit=10,
        extract_cap=10,
        top_k=8,
        exclude_urls=frozenset(),
        seed_passages=[],
        corpus_ids=frozenset(),
        embedder=None,
        vector_store=None,
    )
    assert [p.id for p in result.top] == ["first_p0"]
    excerpt = result.top[0].text
    assert original.startswith(excerpt) and excerpt != original
    assert excerpt in router.requests[0].messages[1].content
    assert nli.premises and all(p in excerpt and original not in p for p in nli.premises)
    assert "Invented" not in result.answer_text
    assert "second_p0" not in result.answer_text
    assert len(result.all_hits) == 2


@pytest.mark.parametrize("reject_unsent_claim", [False, True])
async def test_public_stream_never_exposes_claims_rejected_from_final_provenance(
    reject_unsent_claim,
):
    from disco.core.llm import CompletionResponse, TokenUsage
    from disco.retrieval.streaming import stream_research_answer
    from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit

    class Router(RecordingRouter):
        async def stream_complete(self, request):
            self.requests.append(request)
            for text in ["Paris is the", " capital. [[first_p0]]"]:
                yield StreamChunk(delta_text=text)
            if reject_unsent_claim:
                yield StreamChunk(delta_text=" Invented claim. [[second_p0]]")
            yield StreamChunk(done=True)

        async def complete(self, request):
            return CompletionResponse(
                text="Which sources establish this?\nWhat evidence supports the answer?",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used="arbitrary-model",
            )

    class Ranked:
        async def rerank(self, query, passages, *, top_k):
            return sorted(passages, key=lambda p: p.id)[:top_k]

    first, second = "https://example.test/first", "https://example.test/second"
    original = "Paris is the capital of France. " * 1000
    router = Router()
    frames = [
        frame
        async for frame in stream_research_answer(
            "Which city?",
            router=router,
            search=FakeSearchProvider([hit(first), hit(second)]),
            extraction=FakeExtractionProvider({first: original, second: "Never sent evidence."}),
            reranker=Ranked(),
            nli=FakeNLI({"Paris is the capital": "entail"}),
        )
    ]
    assert not [frame for frame in frames if frame["type"] == "error"]
    tokens = [frame for frame in frames if frame["type"] == "token"]
    assert tokens
    visible = "".join(frame["token"] for frame in tokens)
    assert visible == "Paris is the capital. [[first_p0]]"
    assert "Invented" not in visible and "second_p0" not in visible
    if not reject_unsent_claim:
        # A clean answer retains its existing incremental frame boundaries.
        assert len(tokens) > 1
    final = next(frame["answer"] for frame in frames if frame["type"] == "final")
    assert [passage["id"] for passage in final["passages"]] == ["first_p0"]
    excerpt = final["passages"][0]["text"]
    assert original.startswith(excerpt) and excerpt != original
    assert excerpt in router.requests[0].messages[1].content
    assert final["claims"] and all(c["verdict"] == "supported" for c in final["claims"])
    assert "Invented" not in str(final["blocks"])
    assert frames[-1] == {"type": "state", "status": "finished"}

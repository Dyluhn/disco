"""One-shot ordinary Search contract for ``stream_research_answer``.

The legacy rewriter and max-round arguments remain accepted for compatibility,
but are ignored here. Deep Research owns its separate bounded planning loop.

Fakes: headless, no real network, no real model — scripted answers + scripted NLI
verdicts, consistent with the captured-sample style in research_fakes.py.
"""

from __future__ import annotations

import pytest
from disco.core.llm import CompletionResponse, StreamChunk, TokenUsage
from disco.retrieval import LexicalReranker
from disco.retrieval.streaming import _follow_ups, stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit

# ---------------------------------------------------------------------------
# Fakes specific to this test module
# ---------------------------------------------------------------------------


class AnswerSequenceRouter:
    """Returns scripted answers one-per-stream_complete() call.

    The first call to stream_complete() yields answers[0], the second yields
    answers[1], etc.  complete() (used by _follow_ups with SUMMARIZER role)
    always returns an empty-text response, exercising deterministic follow-up
    fallback while keeping the final-frame assertions stable.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self._idx = 0
        self.stream_calls: list[str] = []  # texts returned in order
        self.stream_requests = []

    async def complete(self, req, *, context=None) -> CompletionResponse:  # type: ignore[override]
        return CompletionResponse(
            text="",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            routing=None,
        )

    async def stream_complete(self, req, *, context=None):  # type: ignore[override]
        text = self._answers[min(self._idx, len(self._answers) - 1)]
        self._idx += 1
        self.stream_calls.append(text)
        self.stream_requests.append(req)
        # Yield each word as a separate chunk, then a terminal done chunk.
        for word in text.split(" "):
            yield StreamChunk(delta_text=word + " ")
        yield StreamChunk(done=True)


class TrackingRewriter:
    """QueryRewriter whose .calls list records every query it received."""

    def __init__(self, paraphrase: str = "reformulated query") -> None:
        self._paraphrase = paraphrase
        self.calls: list[str] = []

    async def rewrite(self, query: str, *, n: int) -> list[str]:
        self.calls.append(query)
        return [self._paraphrase]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEARCH_URL_R0 = "http://example.com/round0"
_SEARCH_URL_R1 = "http://example.com/round1"

# Round-0 answer: a claim that NLI will verdict "neutral" (not supported).
# The passage id must match what FakeExtractionProvider generates for _SEARCH_URL_R0:
#   pid = url.rsplit("/", 1)[-1] → "round0" → passage id "round0_p0"
_ROUND0_ANSWER = "The answer remains unclear. [[round0_p0]]"
_ROUND0_CLAIM_TEXT = "The answer remains unclear"  # what _verify_claims sees

# Round-1 answer: a claim that NLI will verdict "entail" (supported).
# Similarly for _SEARCH_URL_R1 → "round1" → passage id "round1_p0"
_ROUND1_ANSWER = "The sky is definitely blue. [[round1_p0]]"
_ROUND1_CLAIM_TEXT = "The sky is definitely blue"


def _make_search(urls: list[str]) -> FakeSearchProvider:
    return FakeSearchProvider([hit(u) for u in urls])


def _make_extraction(urls: list[str]) -> FakeExtractionProvider:
    """One passage per URL, text chosen so claim extraction works."""
    docs = {}
    for url in urls:
        slug = url.rsplit("/", 1)[-1]
        # passage text is the relevant claim so NLI has a reasonable premise
        docs[url] = f"Source text about {slug}."
    return FakeExtractionProvider(docs)


class _DetailedFakeSearch(FakeSearchProvider):
    async def search_detailed(self, query, **kwargs):
        hits = await self.search(query, **kwargs)
        return hits, {"provider": self.name, "outcome": "ok"}


def _nli_round0_empty_round1_supported() -> FakeNLI:
    """Round-0 claim → 'contradict' (score=0.0, not supported); round-1 → 'entail'.

    NOTE: FakeNLI.score() returns {"entail": 1.0, "neutral": 0.5, "contradict": 0.0}.
    _best_entail() triggers "entail" only when score >= _ENTAIL_MIN (0.5).  A "neutral"
    verdict (score=0.5) satisfies that threshold, so it would map to "supported".
    We must use "contradict" (score=0.0 < 0.5) to produce a NOT-supported claim.
    """
    return FakeNLI(
        {
            _ROUND0_CLAIM_TEXT: "contradict",  # → score 0.0 → not entail → "unsupported"
            _ROUND1_CLAIM_TEXT: "entail",  # → score 1.0 → "supported"
        }
    )


def _nli_all_unsupported() -> FakeNLI:
    """All claims → 'contradict' → score=0.0 → _best_entail returns 'contradict'
    → verdict 'unsupported' → supported count always 0."""
    # FakeNLI default is "neutral" (score=0.5 ≥ _ENTAIL_MIN=0.5 → entail!).
    # We must use "contradict" for both known claim texts to keep supported=0.
    return FakeNLI(
        {
            _ROUND0_CLAIM_TEXT: "contradict",
            _ROUND1_CLAIM_TEXT: "contradict",
        }
    )


async def _collect(gen) -> list[dict]:
    return [frame async for frame in gen]


@pytest.mark.asyncio
async def test_follow_ups_repair_once_then_validate_questions():
    class _Router:
        def __init__(self):
            self.requests = []

        async def complete(self, req, *, context=None):
            self.requests.append(req)
            text = (
                ""
                if len(self.requests) == 1
                else "1. First useful question?\n2) Second useful question?"
            )
            return CompletionResponse(
                text=text,
                usage=TokenUsage(input_tokens=1, output_tokens=1),
                finish_reason="stop",
                model_used="fake",
                routing=None,
            )

    router = _Router()
    follow_ups = await _follow_ups(router, "Why?", "Because.")

    assert follow_ups == ["First useful question?", "Second useful question?"]
    assert [request.attempt for request in router.requests] == [1, 2]
    assert [request.max_tokens for request in router.requests] == [160, 2048]


@pytest.mark.asyncio
async def test_follow_ups_fail_over_to_distinct_contextual_questions():
    router = AnswerSequenceRouter([_ROUND0_ANSWER])

    follow_ups = await _follow_ups(router, "How do safe HTTP methods work?", "Answer")

    assert len(follow_ups) == 3
    assert len(set(follow_ups)) == 3
    assert all("safe HTTP methods" in item for item in follow_ups)


@pytest.mark.asyncio
async def test_empty_reasoning_answer_gets_one_bounded_visible_text_repair():
    router = AnswerSequenceRouter(["", _ROUND0_ANSWER])
    frames = await _collect(
        stream_research_answer(
            "test query",
            router=router,
            search=_make_search([_SEARCH_URL_R0]),
            extraction=_make_extraction([_SEARCH_URL_R0]),
            reranker=LexicalReranker(),
            nli=FakeNLI({_ROUND0_CLAIM_TEXT: "entail"}),
        )
    )

    assert router.stream_calls == ["", _ROUND0_ANSWER]
    assert [request.attempt for request in router.stream_requests] == [1, 2]
    assert [request.max_tokens for request in router.stream_requests] == [1200, 4096]
    assert not any(frame["type"] == "error" for frame in frames)
    final = next(frame for frame in frames if frame["type"] == "final")
    assert final["answer"]["blocks"][0]["text"].strip()


@pytest.mark.asyncio
async def test_double_empty_reasoning_answer_fails_closed_without_blank_final():
    router = AnswerSequenceRouter(["", ""])
    frames = await _collect(
        stream_research_answer(
            "test query",
            router=router,
            search=_make_search([_SEARCH_URL_R0]),
            extraction=_make_extraction([_SEARCH_URL_R0]),
            reranker=LexicalReranker(),
            nli=FakeNLI({_ROUND0_CLAIM_TEXT: "entail"}),
        )
    )

    assert len(router.stream_calls) == 2
    assert not any(frame["type"] == "final" for frame in frames)
    error = next(frame for frame in frames if frame["type"] == "error")
    assert "no visible answer" in error["message"]


# ---------------------------------------------------------------------------
# 1. OFF-path parity guard — round-0 already has supported claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_path_byte_identical_when_round0_has_answer():
    """Frame sequence with rewriter is byte-identical to a no-rewriter run when
    round-0 already produces ≥1 supported claim."""
    reranker = LexicalReranker()
    nli = FakeNLI({_ROUND0_CLAIM_TEXT: "entail"})  # round-0 claim IS supported
    router = AnswerSequenceRouter([_ROUND0_ANSWER])
    search = FakeSearchProvider([hit(_SEARCH_URL_R0)])
    extraction = _make_extraction([_SEARCH_URL_R0])
    rewriter = TrackingRewriter()

    common_kwargs = dict(
        router=router,
        search=search,
        extraction=extraction,
        reranker=reranker,
        nli=nli,
    )

    # Run WITHOUT rewriter (baseline)
    router_no_rw = AnswerSequenceRouter([_ROUND0_ANSWER])
    frames_no_rw = await _collect(
        stream_research_answer("test query", **{**common_kwargs, "router": router_no_rw})
    )

    # Run WITH rewriter (should behave identically)
    router_with_rw = AnswerSequenceRouter([_ROUND0_ANSWER])
    frames_with_rw = await _collect(
        stream_research_answer(
            "test query",
            **{**common_kwargs, "router": router_with_rw},
            rewriter=rewriter,
            max_research_rounds=2,
        )
    )

    # Rewriter must NOT have been called
    assert rewriter.calls == [], f"rewriter.rewrite was called: {rewriter.calls}"

    # Frame types and content must be identical (OFF-path parity)
    def _comparable(frames: list[dict]) -> list[dict]:
        """Strip keys that vary per run (e.g. passage ids generated fresh each time)."""
        out = []
        for f in frames:
            if f["type"] in ("state", "phase"):
                out.append(f)
            elif f["type"] == "token":
                out.append({"type": "token", "token": f["token"], "block_id": f["block_id"]})
            elif f["type"] == "final":
                # Compare the answer text blocks (not passage ids which are run-specific)
                out.append(
                    {
                        "type": "final",
                        "blocks": f["answer"]["blocks"],
                        "claims_verdicts": [c["verdict"] for c in f["answer"]["claims"]],
                        "unsupported_count": f["answer"]["unsupported_count"],
                    }
                )
            else:
                out.append(f)
        return out

    assert _comparable(frames_no_rw) == _comparable(frames_with_rw)

    # Sanity: token frames appear BEFORE final, no reformulating phase
    types = [f["type"] for f in frames_with_rw]
    assert "reformulating" not in [f.get("phase") for f in frames_with_rw]
    assert "token" in types
    assert types[-2] == "final"
    assert types[-1] == "state"


@pytest.mark.asyncio
async def test_detailed_provider_is_one_shot_and_does_not_invoke_query_rewriter():
    router = AnswerSequenceRouter([_ROUND0_ANSWER, _ROUND1_ANSWER])
    rewriter = TrackingRewriter("should not run")
    frames = await _collect(
        stream_research_answer(
            "test query",
            router=router,
            search=_DetailedFakeSearch([hit(_SEARCH_URL_R0)]),
            extraction=_make_extraction([_SEARCH_URL_R0]),
            reranker=LexicalReranker(),
            nli=_nli_all_unsupported(),
            rewriter=rewriter,
            max_research_rounds=2,
        )
    )

    assert len(router.stream_calls) == 1
    assert rewriter.calls == []
    assert not any(frame["type"] == "phase" for frame in frames)
    assert not any(frame["type"] == "final" for frame in frames)

@pytest.mark.asyncio
async def test_legacy_provider_is_one_shot_even_when_research_rounds_requested():
    """Ordinary Search never invokes the rewriter, regardless of provider API."""
    router = AnswerSequenceRouter([_ROUND0_ANSWER, _ROUND1_ANSWER])
    rewriter = TrackingRewriter(paraphrase="must not run")
    frames = await _collect(
        stream_research_answer(
            "impossible query",
            router=router,
            search=FakeSearchProvider([hit(_SEARCH_URL_R0), hit(_SEARCH_URL_R1)]),
            extraction=_make_extraction([_SEARCH_URL_R0, _SEARCH_URL_R1]),
            reranker=LexicalReranker(),
            nli=_nli_all_unsupported(),
            rewriter=rewriter,
            max_research_rounds=9,
        )
    )

    assert rewriter.calls == []
    assert router.stream_calls == [_ROUND0_ANSWER]
    assert not any(frame["type"] == "phase" for frame in frames)
    assert not any(frame["type"] == "final" for frame in frames)
    assert frames[-1]["type"] == "error"


# ---------------------------------------------------------------------------
# 4. max_research_rounds=1 (default) — never reformulates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_max_rounds_never_reformulates():
    """With max_research_rounds=1 (the default), no reformulation happens even
    when round-0 yields 0 supported claims.  This proves the param gates F1 and
    protects all existing callers/tests from unexpected re-search."""
    reranker = LexicalReranker()
    nli = _nli_all_unsupported()  # 0 supported claims — would trigger reformulate if allowed
    search = FakeSearchProvider([hit(_SEARCH_URL_R0)])
    extraction = _make_extraction([_SEARCH_URL_R0])
    router = AnswerSequenceRouter([_ROUND0_ANSWER])
    rewriter = TrackingRewriter()

    frames = await _collect(
        stream_research_answer(
            "query",
            router=router,
            search=search,
            extraction=extraction,
            reranker=reranker,
            nli=nli,
            rewriter=rewriter,
            # max_research_rounds=1 is the default; spelling it out for clarity
            max_research_rounds=1,
        )
    )

    # Rewriter must NOT have been called
    assert rewriter.calls == [], f"rewriter was called with max_research_rounds=1: {rewriter.calls}"

    # No reformulating phase frame
    assert not any(
        f.get("type") == "phase" and f.get("phase") == "reformulating" for f in frames
    ), "reformulating frame emitted with max_research_rounds=1"

    # No unsupported final is certified merely because retries were disabled.
    types = [f["type"] for f in frames]
    assert types[-1] == "error"
    assert "final" not in types

    # Only one generation call (no retry)
    assert len(router.stream_calls) == 1

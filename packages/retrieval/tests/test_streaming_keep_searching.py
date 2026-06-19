"""F1 — keep-searching on no-answer.

Tests the bounded re-search loop added to `stream_research_answer`:
1. Round-0 yields ≥1 supported claim → rewriter.rewrite never called; frame
   sequence is byte-identical to a rewriter=None run  (OFF-path parity guard).
2. Round-0 yields 0 supported claims, round-1 yields ≥1 → rewrite called once;
   final answer is round-1's; a `reformulating` phase frame was emitted; round-0
   tokens were NOT emitted.
3. Both rounds yield 0 supported → emits round-1 final (best-effort) and stops;
   rewrite called ≤2 times total (bound honoured, no loop).
4. max_research_rounds=1 (default) → never reformulates regardless of signal;
   proves the param gates it and protects all existing callers/tests.

Fakes: headless, no real network, no real model — scripted answers + scripted NLI
verdicts, consistent with the captured-sample style in research_fakes.py.
"""

from __future__ import annotations

import pytest
from disco.core.llm import CompletionResponse, StreamChunk, TokenUsage
from disco.retrieval import LexicalReranker
from disco.retrieval.streaming import stream_research_answer
from research_fakes import FakeExtractionProvider, FakeNLI, FakeSearchProvider, hit

# ---------------------------------------------------------------------------
# Fakes specific to this test module
# ---------------------------------------------------------------------------


class AnswerSequenceRouter:
    """Returns scripted answers one-per-stream_complete() call.

    The first call to stream_complete() yields answers[0], the second yields
    answers[1], etc.  complete() (used by _follow_ups with SUMMARIZER role)
    always returns an empty-text response so follow_ups is [] — keeps the
    final-frame assertions simple.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self._idx = 0
        self.stream_calls: list[str] = []  # texts returned in order

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
            _ROUND1_CLAIM_TEXT: "entail",      # → score 1.0 → "supported"
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
                out.append({
                    "type": "final",
                    "blocks": f["answer"]["blocks"],
                    "claims_verdicts": [c["verdict"] for c in f["answer"]["claims"]],
                    "unsupported_count": f["answer"]["unsupported_count"],
                })
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


# ---------------------------------------------------------------------------
# 2. Round-0 no answer → reformulate → round-1 answer used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reformulates_once_when_round0_has_no_supported_claims():
    """When round-0 has 0 supported claims and a rewriter is provided, the
    pipeline emits a reformulating phase frame, calls rewriter.rewrite exactly
    once, and uses the round-1 answer as the final result.  Round-0 tokens are
    NOT emitted."""
    reranker = LexicalReranker()
    nli = _nli_round0_empty_round1_supported()

    # Two different search results so round 1 can return a fresh URL
    multi_search = FakeSearchProvider(
        [hit(_SEARCH_URL_R0), hit(_SEARCH_URL_R1)]
    )
    extraction = _make_extraction([_SEARCH_URL_R0, _SEARCH_URL_R1])

    # Router returns doomed answer on call 0, good answer on call 1
    router = AnswerSequenceRouter([_ROUND0_ANSWER, _ROUND1_ANSWER])
    rewriter = TrackingRewriter(paraphrase="alternative query")

    frames = await _collect(
        stream_research_answer(
            "original query",
            router=router,
            search=multi_search,
            extraction=extraction,
            reranker=reranker,
            nli=nli,
            rewriter=rewriter,
            max_research_rounds=2,
        )
    )

    # rewriter.rewrite called exactly once
    assert len(rewriter.calls) == 1, f"expected 1 rewrite call, got {rewriter.calls}"

    # A `reformulating` phase frame was emitted
    phase_frames = [f for f in frames if f.get("type") == "phase"]
    assert any(f.get("phase") == "reformulating" for f in phase_frames), (
        f"No reformulating frame in: {phase_frames}"
    )

    # Round-0 tokens (from _ROUND0_ANSWER) were NOT emitted
    token_frames = [f for f in frames if f["type"] == "token"]
    emitted_text = "".join(f["token"] for f in token_frames)
    assert "unclear" not in emitted_text, (
        f"Round-0 draft tokens leaked into output: {emitted_text!r}"
    )

    # Round-1 tokens (from _ROUND1_ANSWER) ARE present
    assert "blue" in emitted_text, (
        f"Round-1 answer tokens missing from output: {emitted_text!r}"
    )

    # The final answer uses the round-1 result (has the supported claim)
    final = next(f for f in frames if f["type"] == "final")
    claims = final["answer"]["claims"]
    assert any(c["verdict"] == "supported" for c in claims), (
        f"No supported claim in final answer: {claims}"
    )

    # Frame order: state:running … reformulating … tokens … final … state:finished
    types = [f["type"] for f in frames]
    assert types[0] == "state" and frames[0]["status"] == "running"
    assert types[-1] == "state" and frames[-1]["status"] == "finished"
    assert types[-2] == "final"
    reformulating_idx = next(
        i for i, f in enumerate(frames)
        if f.get("type") == "phase" and f.get("phase") == "reformulating"
    )
    first_token_idx = next(i for i, f in enumerate(frames) if f["type"] == "token")
    assert reformulating_idx < first_token_idx, (
        "reformulating phase must appear before the accepted round's tokens"
    )


# ---------------------------------------------------------------------------
# 3. Bound honoured — both rounds have 0 supported claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_honoured_emits_best_effort_final_when_always_empty():
    """When both rounds have 0 supported claims the pipeline emits the last
    round's answer as best-effort and stops.  rewriter.rewrite is called at
    most once (only one reformulation between round 0 and round 1)."""
    reranker = LexicalReranker()
    nli = _nli_all_unsupported()  # nothing is ever "entail" → supported count always 0
    multi_search = FakeSearchProvider([hit(_SEARCH_URL_R0), hit(_SEARCH_URL_R1)])
    extraction = _make_extraction([_SEARCH_URL_R0, _SEARCH_URL_R1])
    router = AnswerSequenceRouter([_ROUND0_ANSWER, _ROUND1_ANSWER])
    rewriter = TrackingRewriter(paraphrase="another angle")

    frames = await _collect(
        stream_research_answer(
            "impossible query",
            router=router,
            search=multi_search,
            extraction=extraction,
            reranker=reranker,
            nli=nli,
            rewriter=rewriter,
            max_research_rounds=2,
        )
    )

    # Exactly one reformulate between round 0 and round 1
    assert len(rewriter.calls) == 1, (
        f"expected ≤1 rewrite calls, got {rewriter.calls}"
    )

    # A reformulating phase frame was emitted
    assert any(
        f.get("type") == "phase" and f.get("phase") == "reformulating"
        for f in frames
    ), "expected reformulating phase frame"

    # Terminates with a final + state:finished (best-effort)
    types = [f["type"] for f in frames]
    assert types[-1] == "state" and frames[-1]["status"] == "finished"
    assert types[-2] == "final"

    # The last round's answer (round 1) is what's in final (not round 0)
    token_frames = [f for f in frames if f["type"] == "token"]
    emitted_text = "".join(f["token"] for f in token_frames)
    assert "blue" in emitted_text, (
        f"Expected round-1 tokens in output, got: {emitted_text!r}"
    )
    assert "unclear" not in emitted_text, (
        f"Round-0 draft tokens should not appear, got: {emitted_text!r}"
    )

    # No infinite loop — only one generator call after running both rounds
    assert router.stream_calls == [_ROUND0_ANSWER, _ROUND1_ANSWER]


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
        f.get("type") == "phase" and f.get("phase") == "reformulating"
        for f in frames
    ), "reformulating frame emitted with max_research_rounds=1"

    # Pipeline still completes normally — final + finished
    types = [f["type"] for f in frames]
    assert types[-1] == "state" and frames[-1]["status"] == "finished"
    assert types[-2] == "final"

    # Only one generation call (no retry)
    assert len(router.stream_calls) == 1

"""D3 — DR mid-run steer / inject-source: unit tests.

In v2 steering is not a probe scheduler: `pop_steers` is drained at every turn
boundary of the agentic research loop and its strings enter the NEXT turn's
message as priority `USER STEER` guidance, which the lead model acts on with
its own searches. `pop_injected_sources` passages are admitted straight into
the evidence pool (user-provided → exempt from the quality filter and from the
web-source budget).

Covers:
  1. No-hook parity: omitted hooks and explicit None are identical.
  2. Steer: a steer returned at a turn boundary reaches the model as a USER
     STEER line, the search it drives runs (search + observation events, the
     query recorded in completed_probes), and report headings still come from
     the writer, never from the steer text.
  3. Inject-source: an injected Passage is folded into the evidence pool and
     retained on the assembled report's evidence ledger.
  4. Both hooks together, without disturbing the baseline.
  5. The WS client frame types that carry them.

All tests use the scripted-router / fake-retrieval pattern from
`test_research_agent.py` + `test_deep_research.py` to stay hermetic (no
network, no real LLM, no real NLI).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import DeepResearchRun, DepthTier
from disco.retrieval.models import Passage, RetrievalRequest, RetrievalResult, SearchHit
from disco.retrieval.vectorstore import InMemoryVectorStore

# ---- fakes (mirrors test_deep_research.py) -----------------------------------

_TURN0 = (
    '{"brief": "The question asks about X; I will map it before going deep.", '
    '"decision_summary": "Map the question before pivoting.", "coverage": '
    '{"covered": [], "open": ["baseline"], "contradictions_checked": []}, '
    '"queries": ["what is X"], "ready_to_write": false}'
)
_TURN_STEERED = (
    '{"brief": "The question asks about X; I will map it before going deep.", '
    '"decision_summary": "Follow the user steer into risks.", "coverage": '
    '{"covered": [], "open": ["risks"], "contradictions_checked": []}, '
    '"queries": ["risks of X"], "ready_to_write": false}'
)
_DONE = (
    '{"brief": "The evidence is sufficient.", "decision_summary": '
    '"The gathered evidence covers the requested angles.", "coverage": '
    '{"covered": [{"angle": "baseline", "evidence_ids": ["p1_0"]}], '
    '"open": [], "contradictions_checked": ["X criticism"]}, '
    '"queries": [], "ready_to_write": true}'
)
_CLEAN_REVIEW = '{"passes": true, "failures": []}'
_EVIDENCE_ID = re.compile(r"(?m)^\[([\w-]+)\] ")


def _auto_report(prompt: str) -> str:
    """A grounded whole-report draft citing the ids in the writer's own
    evidence block (see `test_deep_research.py` for the same helper)."""
    ids = list(dict.fromkeys(_EVIDENCE_ID.findall(prompt)))[:2]
    if not ids:
        return ""
    body = " ".join(
        "The measured evidence documents the research question with collected "
        f"data and reported figures [[{passage_id}]]."
        for passage_id in ids
    )
    return f"{body} {body}\n\n## Convergent findings\n{body} {body}"


class _FakeRetrieval:
    """RetrievalEngine double: mints two passages per query so evidence is
    attributable to the query that found it."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.queries.append(request.query)
        index = len(self.queries)
        passages = [
            Passage(
                id=f"p{index}_{item}",
                source_url=f"https://example.com/{index}/{item}",
                source_title=f"Source {index}-{item}",
                text=(
                    "Substantive measured evidence documents the research "
                    f"question with collected data and reported figures {index}-{item}."
                ),
            )
            for item in range(2)
        ]
        hits = [
            SearchHit(url=p.source_url, title=p.source_title, snippet="s", rank=i)
            for i, p in enumerate(passages)
        ]
        return RetrievalResult(
            passages=passages, all_hits=hits, extracted=[], issued_queries=[request.query]
        )


class _FakeNLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        return (
            "entail"
            if set(premise.lower().split()) & set(hypothesis.lower().split())
            else "neutral"
        )

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _ScriptedRouter(LLMRouter):
    """Prompt-shape dispatch: research turns replay `turns` (defaulting to
    readiness), the writer gets an auto-grounded report, the review passes it.
    Every research-turn user message is recorded so a test can assert what the
    model was actually told."""

    def __init__(self, turns: list[str] | None = None) -> None:
        self._turns = list(turns or [])
        self.turn_prompts: list[str] = []
        self.calls: list[str] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        joined = "\n".join(message.content for message in request.messages)
        if "FIXED rubric" in joined:
            kind, text = "review", _CLEAN_REVIEW
        elif "WRITE THE REPORT" in joined:
            kind, text = "report", _auto_report(joined)
        else:
            kind = "turn"
            self.turn_prompts.append(request.messages[-1].content)
            text = self._turns.pop(0) if self._turns else _DONE
        self.calls.append(kind)
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


def _collect_events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


def _make_run(
    router: _ScriptedRouter,
    *,
    conversation_id: str = "conv_steer",
    retrieval: _FakeRetrieval | None = None,
) -> DeepResearchRun:
    """Build a DeepResearchRun with fake providers, at the fakes' word scale."""
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=retrieval or _FakeRetrieval(),
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id=conversation_id,
    )
    run._bound = replace(
        run._bound,
        report_min_words=40,
        report_max_words=4_000,
        minimum_research_turns=1,
        minimum_useful_sources=2,
        min_evidence_sources=1,
        min_evidence_themes=1,
    )
    return run


# ---- test 1: OFF-path parity -------------------------------------------------


async def test_no_hooks_run_is_byte_identical_to_baseline() -> None:
    """Both hooks defaulting to None → result is identical to a plain run.

    Runs the engine twice with identical inputs: once without the hook params
    (explicit omission), once with both set to None (explicit None). The
    section titles, bounded_by, and event sequence must be identical — the hook
    plumbing must never change the baseline path.
    """
    # Run A: no hook params (old-style call, pre-D3)
    router_a = _ScriptedRouter([_TURN0, _DONE])
    run_a = _make_run(router_a, conversation_id="conv_a")
    captured_a, emit_a = _collect_events()
    result_a = await run_a.run(emit=emit_a)

    # Run B: explicit None (new-style, hooks param present but OFF)
    router_b = _ScriptedRouter([_TURN0, _DONE])
    run_b = _make_run(router_b, conversation_id="conv_b")
    captured_b, emit_b = _collect_events()
    result_b = await run_b.run(emit=emit_b, pop_steers=None, pop_injected_sources=None)

    # Section structure identical
    assert [s.title for s in result_a.sections] == [s.title for s in result_b.sections]
    assert result_a.sections
    assert result_a.bounded_by == result_b.bounded_by
    assert result_a.bounded_by is None

    # Event sequence identical (kinds, in order)
    assert [k for k, _ in captured_a] == [k for k, _ in captured_b], (
        "event sequence differs between plain and None-hooks run"
    )

    # No USER STEER guidance reached the model on the OFF-path
    assert all("USER STEER" not in prompt for prompt in router_b.turn_prompts)


# ---- test 2: a steer reaches the model and drives the next search ------------


async def test_steer_at_boundary_adds_probe_and_emits_observation() -> None:
    """A steer returned by pop_steers at a turn boundary:
    - enters that turn's message as priority USER STEER guidance
    - the search the model then runs is a real probe → search + observation
      events, and the query lands in completed_probes
    - baseline research still completes (the steer is ADDITIVE)
    - report headings stay writer-owned: the steer text is never a title
    """
    STEER = "What are the risks of X?"
    router = _ScriptedRouter([_TURN0, _TURN_STEERED, _DONE])
    retrieval = _FakeRetrieval()
    run = _make_run(router, conversation_id="conv_steer", retrieval=retrieval)
    captured, emit = _collect_events()

    steer_call_count = {"n": 0}

    def pop_steers() -> list[str]:
        """Return the steer at the SECOND drain (the first turn boundary after
        research started)."""
        steer_call_count["n"] += 1
        return [STEER] if steer_call_count["n"] == 2 else []

    result = await run.run(emit=emit, pop_steers=pop_steers)

    # The steer reached the model as priority guidance on that turn only.
    assert "USER STEER" not in router.turn_prompts[0]
    assert "USER STEER" in router.turn_prompts[1]
    assert STEER in router.turn_prompts[1]

    # Baseline probe AND the steered probe both ran and are recorded.
    assert retrieval.queries == ["what is X", "risks of X"]
    assert result.completed_probes == ["what is X", "risks of X"]

    # The steered probe produced its own search + observation events.
    steered_searches = [p for k, p in captured if k == "search" and p["query"] == "risks of X"]
    assert len(steered_searches) == 1
    observations = [p for k, p in captured if k == "observation"]
    assert len(observations) == 2 and all(p["ok"] for p in observations)

    # Headings come from the writer, not from the steer text.
    assert result.sections
    assert STEER not in {section.title for section in result.sections}
    # bounded_by is NOT set by the steer (it's a normal completion)
    assert result.bounded_by is None


async def test_steer_empty_returns_do_not_change_run() -> None:
    """pop_steers returning [] every time → the run is structurally identical to
    a no-hook run (same probes, no USER STEER guidance anywhere)."""
    router = _ScriptedRouter([_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_steer_empty")
    _, emit = _collect_events()

    result = await run.run(emit=emit, pop_steers=lambda: [])

    assert result.sections
    assert result.completed_probes == ["what is X"]
    assert result.bounded_by is None
    assert all("USER STEER" not in prompt for prompt in router.turn_prompts)


# ---- test 3: inject-source folds a passage into the evidence pool ------------


async def test_inject_source_passage_is_retained_on_the_report() -> None:
    """An injected Passage returned by pop_injected_sources is admitted into the
    run's evidence pool, so the assembled report retains it on the evidence
    ledger (cited_passages or reviewed_passages) — the injection is never
    silently dropped, and being user-provided it is exempt from the quality
    filter that would otherwise reject such a short note.
    """
    INJECTED_ID = "injected-test-001"
    injected_passage = Passage(
        id=INJECTED_ID,
        text="X has a critical vulnerability according to security researchers.",
        source_url="user-injected",
        source_title="User-injected source",
    )

    router = _ScriptedRouter([_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_inject")
    _, emit = _collect_events()

    inject_call_count = {"n": 0}

    def pop_injected() -> list[Passage]:
        inject_call_count["n"] += 1
        return [injected_passage] if inject_call_count["n"] == 1 else []

    result = await run.run(emit=emit, pop_injected_sources=pop_injected)

    assert result.sections
    retained_ids = {
        passage.id for passage in [*result.cited_passages, *result.reviewed_passages]
    }
    assert INJECTED_ID in retained_ids, (
        f"injected passage {INJECTED_ID!r} was lost from the evidence ledger; "
        f"got {retained_ids}"
    )
    # It was also visible to the researcher in the very next evidence digest.
    assert INJECTED_ID in router.turn_prompts[0]


async def test_inject_empty_returns_do_not_change_run() -> None:
    """pop_injected_sources returning [] every time → identical to a no-hook run."""
    router = _ScriptedRouter([_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_inject_empty")
    _, emit = _collect_events()

    result = await run.run(emit=emit, pop_injected_sources=lambda: [])

    assert result.sections
    assert result.completed_probes == ["what is X"]
    assert result.bounded_by is None
    assert all("USER STEER" not in prompt for prompt in router.turn_prompts)


# ---- test 4: both hooks together ---------------------------------------------


async def test_steer_and_inject_compose_correctly() -> None:
    """Steer + inject both active → the steered search runs as its own probe and
    the injected passage is retained in the evidence ledger; neither hook
    disturbs the baseline probe."""
    STEER = "What are the risks of X?"
    INJECTED_ID = "injected-combo-001"
    injected_passage = Passage(
        id=INJECTED_ID,
        text=(
            "Security researchers document a known flaw in X and explain its "
            "operational consequences for deployed systems."
        ),
        source_url="user-injected",
        source_title="User-injected source",
    )

    router = _ScriptedRouter([_TURN0, _TURN_STEERED, _DONE])
    run = _make_run(router, conversation_id="conv_both")
    _, emit = _collect_events()

    steer_calls = {"n": 0}
    inject_calls = {"n": 0}

    def pop_steers() -> list[str]:
        steer_calls["n"] += 1
        return [STEER] if steer_calls["n"] == 2 else []

    def pop_injected() -> list[Passage]:
        inject_calls["n"] += 1
        return [injected_passage] if inject_calls["n"] == 1 else []

    result = await run.run(
        emit=emit, pop_steers=pop_steers, pop_injected_sources=pop_injected
    )

    assert result.sections
    assert result.completed_probes == ["what is X", "risks of X"]
    assert STEER in router.turn_prompts[1]

    retained_ids = {
        passage.id for passage in [*result.cited_passages, *result.reviewed_passages]
    }
    assert INJECTED_ID in retained_ids, (
        f"injected passage {INJECTED_ID!r} was lost from the evidence ledger; "
        f"got {retained_ids}"
    )


# ---- test 5: WS frame type round-trip ----------------------------------------


def test_ws_client_frame_accepts_inject_source_type() -> None:
    """WSClientFrame must accept 'inject_source' with inject_source_text set."""
    from disco.core.wire import WSClientFrame

    frame = WSClientFrame(type="inject_source", inject_source_text="some text")
    assert frame.type == "inject_source"
    assert frame.inject_source_text == "some text"


def test_ws_client_frame_steer_still_valid() -> None:
    """The existing 'steer' frame type must still be valid (no regression)."""
    from disco.core.wire import WSClientFrame

    frame = WSClientFrame(type="steer", steer_text="dig deeper into AI safety")
    assert frame.type == "steer"
    assert frame.steer_text == "dig deeper into AI safety"

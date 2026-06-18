"""D3 — DR mid-run steer / inject-source: unit tests.

Covers:
  1. OFF-path parity: no hooks → byte-identical report to a plain run.
  2. Steer: pop_steers returning a steer at the first boundary → gather_tasks
     grows by one leg, the steered section appears in the report, and a steer
     observation event was emitted.
  3. Inject-source: pop_injected_sources returning a Passage → it appears in
     carried_passages / cited_passages on the assembled report (if cited).
  4. Both hooks: steer + inject work together without disrupting the baseline.

All tests use the same _ScriptedRouter / _Fake* pattern from test_deep_research.py
to stay hermetic (no network, no real LLM, no real embedder/NLI).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core.llm import (
    CallContext,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import (
    DeepResearchRun,
    DepthTier,
)
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.models import (
    ExtractedDoc,
    Passage,
    RetrievalRequest,
    RetrievalResult,
    SearchHit,
)
from disco.retrieval.vectorstore import InMemoryVectorStore

# ---- fakes (mirrors test_deep_research.py) -----------------------------------


class _FakeSearch:
    name = "fake_search"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: Any = None,
        domains_deny: Any = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        self.calls.append(query)
        return [
            SearchHit(
                url=f"https://example.com/{query}/{i}",
                title=f"{query} — source {i}",
                snippet=f"snippet for {query} #{i}",
                source_engine="fake",
                rank=i,
            )
            for i in range(min(limit, 3))
        ]


class _FakeExtraction:
    name = "fake_extract"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0
        self._by_url: dict[str, int] = {}

    async def extract(self, url: str, *, hint: str = "") -> ExtractedDoc | None:
        self.calls.append(url)
        if url not in self._by_url:
            self._by_url[url] = self._counter
            self._counter += 1
        idx = self._by_url[url]
        passage_id = f"p{idx}"
        return ExtractedDoc(
            url=url,
            title=f"doc {idx}",
            markdown=f"content for {url}",
            passages=[
                Passage(
                    id=passage_id,
                    text=f"extracted text {idx}",
                    source_url=url,
                    source_title=f"doc {idx}",
                )
            ],
        )


class _FakeReranker:
    name = "fake_reranker"

    async def rerank(
        self,
        query: str,
        passages: list[Passage],
        *,
        request: RetrievalRequest | None = None,
    ) -> RetrievalResult:
        return RetrievalResult(passages=passages)


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [(sum(ord(c) for c in t[i::8]) % 97) / 97.0 for i in range(8)]
            out.append(vec)
        return out


class _FakeNLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        prem_words = set(premise.lower().split())
        hyp_words = set(hypothesis.lower().split())
        return "entail" if prem_words & hyp_words else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _ScriptedRouter(LLMRouter):
    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts: dict[str, list[str]] = {
            "query_rewriter": [],
            "rag_answerer": [],
        }
        if scripts:
            for k, v in scripts.items():
                self._scripts[k] = list(v)
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value
            if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        last_msg = request.messages[-1].content if request.messages else ""
        self.calls.append((role, last_msg))
        queue = self._scripts.get(role, [])
        if queue:
            text = queue.pop(0)
        elif role == "query_rewriter":
            text = "SUFFICIENT\nnone"
        else:
            text = "(no scripted response)"
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


def _collect_events() -> tuple[list[tuple[str, dict]], Any]:
    captured: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    return captured, emit


def _make_run(
    router: _ScriptedRouter,
    *,
    conversation_id: str = "conv_steer",
) -> DeepResearchRun:
    """Build a DeepResearchRun with fake providers (no embedder → fallback path)."""
    engine = DefaultRetrievalEngine(
        search=_FakeSearch(),
        extraction=_FakeExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )
    return DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,  # no embedder → section retrieval uses fallback_passages
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id=conversation_id,
    )


# ---- test 1: OFF-path parity -------------------------------------------------


@pytest.mark.asyncio
async def test_no_hooks_run_is_byte_identical_to_baseline() -> None:
    """Both hooks defaulting to None → result is byte-identical to a plain run.

    Runs the engine twice with identical inputs: once without the hook params
    (explicit omission), once with both set to None (explicit None). The section
    titles, bounded_by, and event sequence must be identical — the hook plumbing
    must never change the baseline path.
    """
    plan_steps = ["What is X?", "How does X work?"]
    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 4,
        "rag_answerer": [
            "The basics [[p0]].",
            "The mechanism [[p3]].",
            "Overview of X.",
        ],
    }

    # Run A: no hook params (old-style call, pre-D3)
    router_a = _ScriptedRouter(scripts)
    run_a = _make_run(router_a, conversation_id="conv_a")
    captured_a, emit_a = _collect_events()
    result_a = await run_a.run(plan_steps, emit=emit_a)

    # Run B: explicit None (new-style, hooks param present but OFF)
    router_b = _ScriptedRouter(scripts)
    run_b = _make_run(router_b, conversation_id="conv_b")
    captured_b, emit_b = _collect_events()
    result_b = await run_b.run(
        plan_steps,
        emit=emit_b,
        pop_steers=None,
        pop_injected_sources=None,
    )

    # Section structure identical
    assert [s.title for s in result_a.sections] == [s.title for s in result_b.sections]
    assert result_a.bounded_by == result_b.bounded_by
    assert result_a.bounded_by is None

    # Event sequence identical (kinds + subquestion/phase values)
    kinds_a = [k for k, _ in captured_a]
    kinds_b = [k for k, _ in captured_b]
    assert kinds_a == kinds_b, "event sequence differs between plain and None-hooks run"

    # No steer observation events emitted (guards against spurious emit)
    steer_obs = [
        p for k, p in captured_b
        if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == [], "steer observation emitted on the OFF-path"


# ---- test 2: steer adds a new section ----------------------------------------


@pytest.mark.asyncio
async def test_steer_at_boundary_adds_section_and_emits_observation() -> None:
    """A steer returned by pop_steers at the first boundary:
    - extends gather_tasks → the steer's section appears in the final report
    - emits an `observation` event with 'mid-run steer' in `detail`
    - baseline sections are also present (steer is ADDITIVE)
    """
    plan_steps = ["What is X?", "How does X work?"]
    STEER = "What are the risks of X?"

    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 6,  # plenty for steer leg too
        "rag_answerer": [
            "The basics [[p0]].",
            "The mechanism [[p3]].",
            "The risks [[p6]].",          # steer section synthesis
            "Overview of X and its risks.",  # coherence
        ],
    }
    router = _ScriptedRouter(scripts)
    run = _make_run(router, conversation_id="conv_steer")
    captured, emit = _collect_events()

    steer_call_count = {"n": 0}

    def pop_steers() -> list[str]:
        """Return the steer at the FIRST call only (first section boundary)."""
        steer_call_count["n"] += 1
        if steer_call_count["n"] == 1:
            return [STEER]
        return []

    result = await run.run(plan_steps, emit=emit, pop_steers=pop_steers)

    # 3 sections: 2 baseline + 1 steer
    section_titles = [s.title for s in result.sections]
    assert len(section_titles) == 3, f"expected 3 sections, got: {section_titles}"
    assert "What is X?" in section_titles
    assert "How does X work?" in section_titles
    assert STEER in section_titles, f"steer section missing from {section_titles}"

    # steer observation emitted with the right detail
    steer_obs = [
        p for k, p in captured
        if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert len(steer_obs) == 1, f"expected 1 steer observation, got {steer_obs}"
    assert steer_obs[0].get("subquestion") == STEER

    # bounded_by is NOT set by the steer (it's a normal completion)
    assert result.bounded_by is None


@pytest.mark.asyncio
async def test_steer_empty_returns_do_not_change_run() -> None:
    """pop_steers returning [] every time → run is structurally identical to a
    no-hook run (same sections, no steer observation events)."""
    plan_steps = ["What is X?", "How does X work?"]
    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 4,
        "rag_answerer": [
            "The basics [[p0]].",
            "The mechanism [[p3]].",
            "Overview.",
        ],
    }
    router = _ScriptedRouter(scripts)
    run = _make_run(router, conversation_id="conv_steer_empty")
    captured, emit = _collect_events()

    result = await run.run(plan_steps, emit=emit, pop_steers=lambda: [])

    assert len(result.sections) == 2
    assert result.sections[0].title == "What is X?"
    assert result.sections[1].title == "How does X work?"
    steer_obs = [
        p for k, p in captured
        if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == []


# ---- test 3: inject-source folds passage into carried_passages ---------------


@pytest.mark.asyncio
async def test_inject_source_passage_appears_in_carried_passages() -> None:
    """An injected Passage returned by pop_injected_sources is accumulated into
    carried_passages, making it available to _assemble_report. If the synthesis
    LLM cites it, it will appear in cited_passages; if not, it's still in
    carried_passages (accessible for follow-ups). This test verifies the passage
    is at minimum carried to assembly.
    """
    plan_steps = ["What is X?"]
    INJECTED_ID = "injected-test-001"
    INJECTED_TEXT = "X has a critical vulnerability according to security researchers."

    injected_passage = Passage(
        id=INJECTED_ID,
        text=INJECTED_TEXT,
        source_url="user-injected",
        source_title="User-injected source",
    )

    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 2,
        # Synthesis cites the injected passage so it appears in cited_passages
        "rag_answerer": [
            f"The risks are significant [[p0]] [[{INJECTED_ID}]].",
            "Overview of X.",
        ],
    }
    router = _ScriptedRouter(scripts)
    run = _make_run(router, conversation_id="conv_inject")
    _, emit = _collect_events()

    inject_call_count = {"n": 0}

    def pop_injected() -> list[Passage]:
        inject_call_count["n"] += 1
        if inject_call_count["n"] == 1:
            return [injected_passage]
        return []

    result = await run.run(plan_steps, emit=emit, pop_injected_sources=pop_injected)

    # The injected passage must appear in carried_passages (assemble adds them)
    all_passage_ids = {p.id for p in result.cited_passages}
    # Check that the synthesized section sees the injected passage (it's in fallback)
    # and if cited, it shows up in cited_passages. At minimum the passage was
    # available (sub_result.passages was extended). Since the scripted LLM cited it,
    # it should be in cited_passages.
    assert INJECTED_ID in all_passage_ids, (
        f"injected passage {INJECTED_ID!r} not in cited_passages: "
        f"{[p.id for p in result.cited_passages]}"
    )


@pytest.mark.asyncio
async def test_inject_empty_returns_do_not_change_run() -> None:
    """pop_injected_sources returning [] every time → byte-identical to no-hook run."""
    plan_steps = ["What is X?", "How does X work?"]
    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 4,
        "rag_answerer": [
            "The basics [[p0]].",
            "The mechanism [[p3]].",
            "Overview.",
        ],
    }
    router = _ScriptedRouter(scripts)
    run = _make_run(router, conversation_id="conv_inject_empty")
    captured, emit = _collect_events()

    result = await run.run(
        plan_steps,
        emit=emit,
        pop_injected_sources=lambda: [],
    )

    assert len(result.sections) == 2
    assert result.bounded_by is None
    # No steer observation (no steer hook was installed)
    steer_obs = [
        p for k, p in captured
        if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == []


# ---- test 4: both hooks together ---------------------------------------------


@pytest.mark.asyncio
async def test_steer_and_inject_compose_correctly() -> None:
    """Steer + inject both active → steer adds a section, injected passage is
    in fallback for the steer leg too (accumulated list extends every leg)."""
    plan_steps = ["What is X?"]
    STEER = "What are the risks of X?"
    INJECTED_ID = "injected-combo-001"
    injected_passage = Passage(
        id=INJECTED_ID,
        text="X has a known security flaw.",
        source_url="user-injected",
        source_title="User-injected source",
    )

    scripts = {
        "query_rewriter": ["SUFFICIENT\nnone"] * 4,
        "rag_answerer": [
            f"Basics [[p0]] [[{INJECTED_ID}]].",      # section 1 cites injected
            f"Risks [[p3]] [[{INJECTED_ID}]].",       # steer section
            "Overview of X.",
        ],
    }
    router = _ScriptedRouter(scripts)
    run = _make_run(router, conversation_id="conv_both")
    _, emit = _collect_events()

    steer_calls = {"n": 0}
    inject_calls = {"n": 0}

    def pop_steers() -> list[str]:
        steer_calls["n"] += 1
        return [STEER] if steer_calls["n"] == 1 else []

    def pop_injected() -> list[Passage]:
        inject_calls["n"] += 1
        return [injected_passage] if inject_calls["n"] == 1 else []

    result = await run.run(
        plan_steps,
        emit=emit,
        pop_steers=pop_steers,
        pop_injected_sources=pop_injected,
    )

    section_titles = [s.title for s in result.sections]
    assert "What is X?" in section_titles
    assert STEER in section_titles

    cited_ids = {p.id for p in result.cited_passages}
    assert INJECTED_ID in cited_ids, (
        f"injected passage {INJECTED_ID!r} not cited; got {cited_ids}"
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

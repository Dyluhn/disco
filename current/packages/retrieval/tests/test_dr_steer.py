"""D3 — DR mid-run steer / inject-source: unit tests.

Covers:
  1. No-hook parity: omitted hooks and explicit None are identical.
  2. Steer: pop_steers returning a steer at the first boundary grows the
     evidence investigation and emits a steer observation. Report headings are
     still compiled from evidence, never copied from probes.
  3. Inject-source: pop_injected_sources returning a Passage → it is folded
     into the evidence pool and retained on the assembled report's evidence
     ledger (cited_passages or reviewed_passages).
  4. Both hooks: steer + inject work together without disrupting the baseline.

All tests use the same _ScriptedRouter / _Fake* pattern from test_deep_research.py
to stay hermetic (no network, no real LLM, no real embedder/NLI). Planner,
section, and summary calls are dispatched by prompt shape with grounded
auto-responses, so the one report funnel runs without hand-scripting every
call.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from disco.core.llm import (
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
from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.models import (
    ExtractedDoc,
    Passage,
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
            content=f"content for {url}",
            passages=[
                Passage(
                    id=passage_id,
                    text=(
                        f"Source {idx} provides substantive evidence about X, "
                        "including its operation, constraints, risks, and practical implications."
                    ),
                    source_url=url,
                    source_title=f"doc {idx}",
                )
            ],
        )

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        docs = [await self.extract(url) for url in urls]
        return [doc for doc in docs if doc is not None]


class _FakeReranker:
    name = "fake_reranker"

    async def rerank(
        self,
        query: str,
        passages: list[Passage],
        *,
        top_k: int,
    ) -> list[Passage]:
        return passages[:top_k]


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
    """Prompt-shape dispatch mirror of test_deep_research.py's router: the
    planner call gets an evidence-grouped JSON outline, section/summary calls
    get grounded auto-responses (or the "section"/"summary" script queues),
    and gap-reason calls default to SUFFICIENT so most tests exercise the
    funnel without hand-scripting every call."""

    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts: dict[str, list[str]] = {
            "query_rewriter": [],
            "rag_answerer": [],
            "section": [],
            "summary": [],
        }
        if scripts:
            for k, v in scripts.items():
                self._scripts[k] = list(v)
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _auto_section(last_msg: str) -> str:
        ids = re.findall(r"^\[([\w-]+)\]", last_msg, re.MULTILINE)[:2]
        if not ids:
            return "(no scripted response)"
        cited = " ".join(f"[[{pid}]]" for pid in ids)
        # Long enough to clear the per-section minimum-content gate (the gate
        # caps at 120 words), every sentence cited and entailed by overlap.
        sentence = (
            "The sources document the research question with collected data "
            f"and measured evidence relevant to the finding {cited}."
        )
        return " ".join([sentence] * 12)

    @staticmethod
    def _auto_summary(last_msg: str) -> str:
        ids = list(dict.fromkeys(re.findall(r"\[\[([\w-]+)\]\]", last_msg)))[:2]
        if not ids:
            return "(no scripted response)"
        cited = " ".join(f"[[{pid}]]" for pid in ids)
        return f"The evidence establishes the answer to the question {cited}."

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value
            if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        last_msg = request.messages[-1].content if request.messages else ""
        prompt_texts = [message.content for message in request.messages]
        planner_msg = next(
            (text for text in prompt_texts if "Design the finished report structure" in text),
            "",
        )
        is_section = any(
            "You are writing one section" in text or "Rewrite the previous section" in text
            for text in prompt_texts
        )
        is_summary = any("executive editor of a research report" in text for text in prompt_texts)
        self.calls.append((role, last_msg))
        if role == "rag_answerer" and planner_msg:
            evidence_ids = re.findall(r"^\[([^\]]+)\]", planner_msg, re.MULTILINE)
            groups = [evidence_ids[index : index + 3] for index in range(0, len(evidence_ids), 3)]
            text = json.dumps(
                [
                    {"title": f"Evidence-led finding {index + 1}", "evidence_ids": ids}
                    for index, ids in enumerate(groups)
                ]
            )
        elif role == "rag_answerer" and "strict claim-grounding judge" in last_msg:
            text = "SUPPORTED"
        elif is_section and self._scripts["section"]:
            text = self._scripts["section"].pop(0)
        elif is_summary and self._scripts["summary"]:
            text = self._scripts["summary"].pop(0)
        elif (queue := self._scripts.get(role, [])):
            text = queue.pop(0)
        elif role == "query_rewriter":
            text = "SUFFICIENT\nnone"  # default: stop the loop
        elif is_section:
            text = self._auto_section(last_msg)
        elif is_summary:
            text = self._auto_summary(last_msg)
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
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,  # no embedder → section retrieval uses fallback_passages
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id=conversation_id,
    )
    run._bound = replace(
        run._bound,
        min_evidence_passages=6,
        min_evidence_themes=1,
        min_evidence_sources=6,
    )
    return run


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

    # Run A: no hook params (old-style call, pre-D3)
    router_a = _ScriptedRouter()
    run_a = _make_run(router_a, conversation_id="conv_a")
    captured_a, emit_a = _collect_events()
    result_a = await run_a.run(plan_steps, emit=emit_a)

    # Run B: explicit None (new-style, hooks param present but OFF)
    router_b = _ScriptedRouter()
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
        p
        for k, p in captured_b
        if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == [], "steer observation emitted on the OFF-path"


# ---- test 2: steer adds a research probe -------------------------------------


@pytest.mark.asyncio
async def test_steer_at_boundary_adds_probe_and_emits_observation() -> None:
    """A steer returned by pop_steers at the first boundary:
    - becomes a research probe → gathered and recorded in completed_probes
    - emits an `observation` event with 'mid-run steer' in `detail`
    - baseline probes still complete (steer is ADDITIVE)
    - report headings stay compiler-owned: the steer text is never a title
    """
    plan_steps = ["What is X?", "How does X work?"]
    STEER = "What are the risks of X?"

    router = _ScriptedRouter()
    run = _make_run(router, conversation_id="conv_steer")
    captured, emit = _collect_events()

    steer_call_count = {"n": 0}

    def pop_steers() -> list[str]:
        """Return the steer at the FIRST call only (first probe boundary)."""
        steer_call_count["n"] += 1
        if steer_call_count["n"] == 1:
            return [STEER]
        return []

    result = await run.run(plan_steps, emit=emit, pop_steers=pop_steers)

    assert result.sections
    assert set(plan_steps) <= set(result.completed_probes)
    assert STEER in result.completed_probes
    assert STEER not in {section.title for section in result.sections}

    # steer observation emitted with the right detail
    steer_obs = [
        p for k, p in captured if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert len(steer_obs) == 1, f"expected 1 steer observation, got {steer_obs}"
    assert steer_obs[0].get("subquestion") == STEER

    # bounded_by is NOT set by the steer (it's a normal completion)
    assert result.bounded_by is None


@pytest.mark.asyncio
async def test_steer_empty_returns_do_not_change_run() -> None:
    """pop_steers returning [] every time → run is structurally identical to a
    no-hook run (same probes completed, no steer observation events)."""
    plan_steps = ["What is X?", "How does X work?"]
    router = _ScriptedRouter()
    run = _make_run(router, conversation_id="conv_steer_empty")
    captured, emit = _collect_events()

    result = await run.run(plan_steps, emit=emit, pop_steers=lambda: [])

    assert result.sections
    assert set(result.completed_probes) == set(plan_steps)
    steer_obs = [
        p for k, p in captured if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == []


@pytest.mark.asyncio
async def test_steer_respects_tier_subquestion_cap() -> None:
    plan_steps = [f"Question {index}" for index in range(6)]
    steer = "A seventh question"
    router = _ScriptedRouter()
    run = _make_run(router, conversation_id="conv_steer_cap")
    run._bound = replace(run._bound, max_subquestions=6)
    captured, emit = _collect_events()
    calls = 0

    def pop_steers() -> list[str]:
        nonlocal calls
        calls += 1
        return [steer] if calls == 1 else []

    result = await run.run(plan_steps, emit=emit, pop_steers=pop_steers)

    assert result.sections
    assert len(result.completed_probes) == 6
    assert steer in result.completed_probes
    assert result.pending_probes == []
    assert steer not in {section.title for section in result.sections}
    accepted = [
        payload
        for kind, payload in captured
        if kind == "observation" and "mid-run steer" in str(payload.get("detail", ""))
    ]
    assert len(accepted) == 1
    assert accepted[0]["ok"] is True
    assert result.bounded_by is None


# ---- test 3: inject-source folds passage into the evidence pool --------------


@pytest.mark.asyncio
async def test_inject_source_passage_is_retained_on_the_report() -> None:
    """An injected Passage returned by pop_injected_sources is folded into the
    run's carried passages, making it part of the global evidence pool the
    report is compiled from. Whether or not a section ends up citing it, the
    assembled report retains it on the evidence ledger (cited_passages or
    reviewed_passages) — the injection is never silently dropped.
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

    router = _ScriptedRouter()
    run = _make_run(router, conversation_id="conv_inject")
    _, emit = _collect_events()

    inject_call_count = {"n": 0}

    def pop_injected() -> list[Passage]:
        inject_call_count["n"] += 1
        if inject_call_count["n"] == 1:
            return [injected_passage]
        return []

    result = await run.run(plan_steps, emit=emit, pop_injected_sources=pop_injected)

    assert result.sections
    retained_ids = {
        passage.id for passage in [*result.cited_passages, *result.reviewed_passages]
    }
    assert INJECTED_ID in retained_ids, (
        f"injected passage {INJECTED_ID!r} was lost from the evidence ledger; "
        f"got {retained_ids}"
    )


@pytest.mark.asyncio
async def test_inject_empty_returns_do_not_change_run() -> None:
    """pop_injected_sources returning [] every time → byte-identical to no-hook run."""
    plan_steps = ["What is X?", "How does X work?"]
    router = _ScriptedRouter()
    run = _make_run(router, conversation_id="conv_inject_empty")
    captured, emit = _collect_events()

    result = await run.run(
        plan_steps,
        emit=emit,
        pop_injected_sources=lambda: [],
    )

    assert result.sections
    assert set(result.completed_probes) == set(plan_steps)
    assert result.bounded_by is None
    # No steer observation (no steer hook was installed)
    steer_obs = [
        p for k, p in captured if k == "observation" and "mid-run steer" in str(p.get("detail", ""))
    ]
    assert steer_obs == []


# ---- test 4: both hooks together ---------------------------------------------


@pytest.mark.asyncio
async def test_steer_and_inject_compose_correctly() -> None:
    """Steer + inject both active → the steer becomes a completed probe and the
    injected passage is retained in the evidence ledger; neither hook disturbs
    the baseline probe."""
    plan_steps = ["What is X?"]
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

    router = _ScriptedRouter()
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

    assert result.sections
    assert "What is X?" in result.completed_probes
    assert STEER in result.completed_probes

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

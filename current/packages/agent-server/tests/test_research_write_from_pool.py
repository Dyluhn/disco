"""POST /research/write-from-pool — write the report again, without researching.

A writer change used to be judged by re-running the whole ~90-minute research
loop, and every run gathers a different pool, so two writers were never compared
on the same evidence. The engine now saves the writer's entire input as one
file; this route hands that file back to the writer.

What the tests pin:

  1. A saved pool produces the ordinary terminal ReportEvent + FINISHED, and
     the report's passages are the pool's CITED subset.
  2. The model calls are the writer's only — draft / review / rework. A
     `research_turn` stage would mean the replay had gone back to the web,
     which is the whole thing this route exists to avoid.
  3. A pool posted INLINE works identically, which is what makes the file
     portable: a pool captured on one machine replays on another.
  4. An id that names no pool is a 404, and a body that names neither source
     (or both) is a 400 — never a run that starts and fails later.

Hermetic: the retrieval deps, preflight, and router/NLI are doubles, so no
provider, encoder, or model is reached.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server._deep_research_service_parts import (
    write_from_pool as write_from_pool_parts,
)
from disco.core import ConversationStatus, ReportEvent, SqliteEventStore, StatusEvent
from disco.core.inspect import registry
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.deep_research.pool import pool_document, write_pool
from disco.retrieval.models import Passage
from fastapi.testclient import TestClient

CID = "conv_write_from_pool_01"
POOL_ID = "conv_saved_run_01"

_BODY = (
    "Measured evidence relevant to the research question, with reported "
    "figures and collected data from 2024 documented in the record."
)

#: The alias lines the writer's evidence block renders (`[s1] …`). The draft is
#: built from the ids in the writer's OWN prompt so the citations are real.
_EVIDENCE_ID = re.compile(r"(?m)^\[([\w-]+)\] ")

_CLEAN_REVIEW = '{"passes": true, "failures": []}'


def _pool_passages() -> list[Passage]:
    return [
        Passage(
            id=f"p{index}",
            source_url=f"https://example.com/source-{index}",
            source_title=f"Source {index}",
            text=f"{_BODY} Record {index}.",
        )
        for index in range(2)
    ]


def _saved_document() -> dict[str, Any]:
    return pool_document(
        POOL_ID,
        query="the state of X",
        depth_tier="standard_deep",
        recency_window=None,
        outcome=ResearchOutcome(
            brief="The question asks about X; map the players and the numbers.",
            passages=_pool_passages(),
            all_hits=[],
            trail=[{"kind": "search", "query": "what is X", "result": "ok"}],
            bounded_by=None,
            coverage={"covered": [{"angle": "overview", "evidence_ids": ["p0"]}], "open": []},
        ),
    )


def _report_markdown(prompt: str) -> str:
    """A whole-report draft citing the ids in the writer's own evidence block —
    genuine grounding against the overlap NLI, not markup."""
    ids = list(dict.fromkeys(_EVIDENCE_ID.findall(prompt)))[:2]
    if not ids:
        return ""
    first, second = ids[0], ids[-1]
    summary = (
        "The measured evidence answers the research question with collected data "
        f"and reported figures [[{first}]]. Independent evidence adds context "
        f"about the scope of the documented record [[{second}]]."
    )
    findings = " ".join(
        f"The collected data and reported figures document the measured record "
        f"in setting {n} [[{first}]] [[{second}]]."
        for n in range(6)
    )
    limits = " ".join(
        f"The reported figures cover only the documented settings, which limits "
        f"the measured reading {n} [[{second}]]."
        for n in range(6)
    )
    return f"{summary}\n\n## Convergent findings\n{findings}\n\n## Limits of the data\n{limits}"


class _StubNLI:
    """Entails when premise and claim share a word; otherwise neutral."""

    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        shared = set(premise.lower().split()) & set(hypothesis.lower().split())
        return "entail" if shared else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _WriterRouter(LLMRouter):
    """Answers only the calls the WRITER makes, dispatched by prompt shape. A
    research turn would land in `other`, which the tests assert stays empty."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    @staticmethod
    def _kind(request: CompletionRequest) -> str:
        joined = "\n".join(message.content for message in request.messages)
        if "Your draft has been reviewed" in request.messages[-1].content:
            return "rework"
        if "FIXED rubric" in joined:
            return "review"
        if "WRITE THE REPORT" in joined:
            return "report"
        return "other"

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        kind = self._kind(request)
        self.kinds.append(kind)
        joined = "\n".join(message.content for message in request.messages)
        text = _CLEAN_REVIEW if kind == "review" else _report_markdown(joined)
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


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    runtime = ConversationRuntime(store)
    runtime.settings._set_surface(CID, "deep_research")
    store.create_conversation(CID, owner_id="local")
    return runtime


@pytest.fixture
def writer_doubles(monkeypatch: pytest.MonkeyPatch) -> _WriterRouter:
    """Stand in for everything below the writer: the retrieval deps, the
    preflight, and the router/NLI pair the server would build live."""
    router = _WriterRouter()

    async def fake_deps(service: Any, conversation_id: str) -> tuple[Any, Any, Any]:
        del service, conversation_id
        return {"nli": _StubNLI(), "reranker": None, "embedder": None}, frozenset(), ()

    async def fake_preflight(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    def fake_router_and_engine(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        del args, kwargs
        return router, None

    monkeypatch.setattr(write_from_pool_parts, "build_retrieval_deps", fake_deps)
    monkeypatch.setattr(write_from_pool_parts, "run_preflight", fake_preflight)
    monkeypatch.setattr(write_from_pool_parts, "build_router_and_engine", fake_router_and_engine)
    return router


def _post(body: dict[str, Any], runtime: ConversationRuntime, store: SqliteEventStore):
    client = TestClient(create_app(store, runtime=runtime))
    return client.post("/research/write-from-pool", json=body)


def _events(store: SqliteEventStore) -> list[Any]:
    store._write_lock = asyncio.Lock()
    return asyncio.run(store.get_events(CID))


def test_saved_pool_produces_the_ordinary_terminal_report(
    monkeypatch: pytest.MonkeyPatch, writer_doubles: _WriterRouter
) -> None:
    """The replay ends exactly where a run ends: one ReportEvent whose passages
    are the pool's CITED subset, then FINISHED."""
    write_pool(
        POOL_ID,
        query="the state of X",
        depth_tier="standard_deep",
        recency_window=None,
        outcome=ResearchOutcome(
            brief="The question asks about X.",
            passages=_pool_passages(),
            all_hits=[],
            trail=[{"kind": "search", "query": "what is X", "result": "ok"}],
            bounded_by=None,
            coverage={"covered": [], "open": []},
        ),
    )
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)

    response = _post({"conversation_id": CID, "pool_id": POOL_ID}, runtime, store)

    assert response.status_code == 200, response.text
    assert response.json()["pool_id"] == POOL_ID
    events = _events(store)
    reports = [e for e in events if isinstance(e, ReportEvent)]
    assert len(reports) == 1, "a replay emits exactly one terminal report"
    cited = [p["id"] for p in reports[0].passages]
    pool_ids = [p.id for p in _pool_passages()]
    assert cited and set(cited) <= set(pool_ids)
    assert reports[0].depth_tier == "standard_deep"
    statuses = [e.status for e in events if isinstance(e, StatusEvent)]
    assert statuses[-1] == ConversationStatus.FINISHED


def test_replay_calls_only_the_writer(
    monkeypatch: pytest.MonkeyPatch, writer_doubles: _WriterRouter
) -> None:
    """No research happened: every model call is a writer stage, and the
    recorded inspect stages are the writer's own."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    write_pool(
        POOL_ID,
        query="the state of X",
        depth_tier="standard_deep",
        recency_window=None,
        outcome=ResearchOutcome(
            brief="The question asks about X.",
            passages=_pool_passages(),
            all_hits=[],
            trail=[],
            bounded_by=None,
            coverage={},
        ),
    )
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)

    response = _post({"conversation_id": CID, "pool_id": POOL_ID}, runtime, store)

    assert response.status_code == 200, response.text
    assert "other" not in writer_doubles.kinds, "a research turn must never be issued"
    assert set(writer_doubles.kinds) <= {"report", "review", "rework"}
    snapshot = registry().snapshot(CID) or {}
    stages = [record.get("stage") for record in snapshot.get("model_io", [])]
    assert stages, "the writer's model I/O must be recorded under the conversation"
    assert "research_turn" not in stages
    assert all(str(stage).startswith("report_") for stage in stages), stages


def test_inline_pool_replays_without_the_file_on_this_server(
    monkeypatch: pytest.MonkeyPatch, writer_doubles: _WriterRouter
) -> None:
    """The pool document is the whole contract, so a pool captured elsewhere
    replays here without ever being saved to this server's data directory."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)

    response = _post({"conversation_id": CID, "pool": _saved_document()}, runtime, store)

    assert response.status_code == 200, response.text
    assert [e for e in _events(store) if isinstance(e, ReportEvent)]


def test_unknown_pool_id_is_a_named_404(
    monkeypatch: pytest.MonkeyPatch, writer_doubles: _WriterRouter
) -> None:
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)

    response = _post({"conversation_id": CID, "pool_id": "no-such-pool"}, runtime, store)

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "pool_not_found"
    assert not [e for e in _events(store) if isinstance(e, ReportEvent)]


def test_body_must_name_exactly_one_pool_source(
    monkeypatch: pytest.MonkeyPatch, writer_doubles: _WriterRouter
) -> None:
    """Neither source, or both, is refused at the edge rather than guessed at."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store)

    for body in (
        {"conversation_id": CID},
        {"conversation_id": CID, "pool_id": POOL_ID, "pool": _saved_document()},
    ):
        response = _post(body, runtime, store)
        assert response.status_code == 400, body.keys()
        assert response.json()["detail"]["reason"] == "pool_required"

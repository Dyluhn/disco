"""Cross-contract acceptance gate — retrieval-grounding-contract.md §8.5.

Composes FOUR contracts headless: a Research-scope loop (loop) calls the
search/extract tools (tools) whose capability handlers back onto the retrieval
engine (retrieval), then the grounding pipeline runs the RAG_ANSWERER (router)
and NLI verification → a GroundedAnswer with full provenance. Asserts the
provider key never reaches the tool observations or the answer (tool §6).
"""

from __future__ import annotations

from disco.core import ConversationStatus, SqliteEventStore, ToolCall
from disco.core.llm import OperatingMode
from disco.core.loop import AgentLoop, AgentStep, NeverConfirm, NullSecurityAnalyzer
from disco.core.view import NoOpCondenser
from disco.retrieval import (
    CrossEncoderNLIVerifier,
    DefaultRetrievalEngine,
    GroundingPipeline,
    LexicalReranker,
    RetrievalRequest,
)
from disco.tools import (
    CapabilityBroker,
    DefaultToolExecutor,
    InMemorySecretsStore,
    build_default_registry,
    research_scope,
)
from research_fakes import FakeExtractionProvider, FakeRouter, FakeSearchProvider, hit

SENTINEL_KEY = "SEARXNG-PROVIDER-KEY-DO-NOT-LEAK"
CID = "conv"


class _ScriptedAgent:
    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    async def step(self, view, tools, *, mode, overflow_signal, on_stream=None,
                   temperature=None, assist=False, attempt: int = 1, provider_prefs=None):
        i = self.calls
        self.calls += 1
        return self._steps[min(i, len(self._steps) - 1)]


class _FakeSummarizer:
    async def summarize(self, messages):
        return "[summary]"


async def test_research_flow_composes_loop_tools_retrieval_router():
    # Retrieval engine over fake providers.
    search_provider = FakeSearchProvider([hit("http://x/cats", title="Cats")])
    extraction = FakeExtractionProvider({"http://x/cats": "Cats are mammals and popular pets."})
    engine = DefaultRetrievalEngine(search_provider, extraction, LexicalReranker())

    # Orchestrator-side capability handlers back onto the engine; the provider
    # key lives in the closure (secrets store) and never crosses to the tool.
    secrets = InMemorySecretsStore({"SEARCH_KEY": SENTINEL_KEY})

    async def search_handler(*, query, limit=5):
        _ = secrets.get("SEARCH_KEY")  # used orchestrator-side only
        res = await engine.retrieve(RetrievalRequest(query=query, depth="shallow", top_k=limit))
        return [{"url": h.url, "title": h.title} for h in res.all_hits]

    async def extract_handler(*, url):
        return (await extraction.extract(url)).content

    broker = CapabilityBroker()
    broker.register("search", search_handler)
    broker.register("extract", extract_handler)

    executor = DefaultToolExecutor(build_default_registry(), research_scope(), broker=broker)
    store = SqliteEventStore(":memory:")
    agent = _ScriptedAgent(
        [
            AgentStep(
                thought="search",
                tool_call=ToolCall(tool_name="search", arguments={"query": "cats", "limit": 3}),
            ),
            AgentStep(
                thought="read",
                tool_call=ToolCall(tool_name="extract", arguments={"url": "http://x/cats"}),
            ),
            AgentStep(thought="done", finished=True),
        ]
    )
    loop = AgentLoop(
        CID,
        store,
        agent,
        executor,
        None,
        NullSecurityAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        _FakeSummarizer(),
        mode=OperatingMode.INTERACTIVE,
    )

    await loop.send_message("what are cats?")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    observations = [e for e in events if e.kind.value == "observation"]
    assert len(observations) == 2  # search + extract
    # The provider key never appears in any observation the agent/model sees.
    for obs in observations:
        assert SENTINEL_KEY not in obs.tool_result.content
        assert SENTINEL_KEY not in str(obs.tool_result.structured)

    # Grounding over the engine's retrieval → a GroundedAnswer with provenance.
    retrieval = await engine.retrieve(RetrievalRequest(query="what are cats?", depth="shallow"))
    router = FakeRouter(answerer_text="Cats are mammals [cats_p0].")
    grounded = await GroundingPipeline(router, CrossEncoderNLIVerifier()).answer(
        "what are cats?", retrieval
    )

    assert grounded.passages, "grounded answer carries cited provenance"
    assert grounded.all_hits, "all_hits populated for the source panel"
    assert grounded.unsupported_count == 0  # the claim is entailed by its passage
    assert "Cats are mammals" in grounded.answer_markdown
    assert SENTINEL_KEY not in grounded.answer_markdown

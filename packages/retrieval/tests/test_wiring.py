"""Shipped wiring + Research flow — retrieval-grounding-contract.md §5/§6.

Exercises the reusable wiring (item 5) through the REAL tools executor + the
Research-surface composition — the shippable form of the §8.5 in-test wiring.
"""

from __future__ import annotations

from disco.core import ToolCall
from disco.retrieval import (
    CrossEncoderNLIVerifier,
    DefaultRetrievalEngine,
    GroundingPipeline,
    LexicalReranker,
    research_answer,
    retrieval_capability_handlers,
)
from disco.tools import (
    CapabilityBroker,
    DefaultToolExecutor,
    InMemorySecretsStore,
    build_default_registry,
    research_scope,
)
from research_fakes import FakeExtractionProvider, FakeRouter, FakeSearchProvider, hit

KEY = "FIRECRAWL-KEY-DO-NOT-LEAK"


def _providers():
    search = FakeSearchProvider([hit("http://x/cats", title="Cats", snippet="snip")])
    extraction = FakeExtractionProvider({"http://x/cats": "Cats are mammals and pets."})
    return search, extraction


async def test_capability_handlers_drive_the_real_search_tool_without_leaking_keys():
    search, extraction = _providers()
    # A provider that closes over a secret key (orchestrator-held).
    secrets = InMemorySecretsStore({"FIRECRAWL_KEY": KEY})

    class KeyedExtraction(FakeExtractionProvider):
        async def extract(self, url):
            _ = secrets.get("FIRECRAWL_KEY")  # used provider-side only
            return await super().extract(url)

    handlers = retrieval_capability_handlers(search, KeyedExtraction({"http://x/cats": "body"}))
    broker = CapabilityBroker()
    for name, h in handlers.items():
        broker.register(name, h)

    ex = DefaultToolExecutor(build_default_registry(), research_scope(), broker=broker)
    res = await ex.execute(ToolCall(tool_name="search", arguments={"query": "cats"}))
    assert res.success
    assert "http://x/cats" in res.content  # discovery results surfaced
    assert KEY not in res.content and KEY not in str(res.structured)  # key never leaks

    ext = await ex.execute(ToolCall(tool_name="extract", arguments={"url": "http://x/cats"}))
    assert ext.success and KEY not in ext.content


async def test_research_answer_composes_retrieve_and_ground():
    search, extraction = _providers()
    engine = DefaultRetrievalEngine(search, extraction, LexicalReranker())
    router = FakeRouter(answerer_text="Cats are mammals [cats_p0].")
    grounding = GroundingPipeline(router, CrossEncoderNLIVerifier())

    grounded = await research_answer("what are cats?", engine, grounding, depth="shallow")
    assert grounded.passages  # provenance
    assert grounded.all_hits  # source panel
    assert "Cats are mammals" in grounded.answer_markdown
    assert grounded.unsupported_count == 0

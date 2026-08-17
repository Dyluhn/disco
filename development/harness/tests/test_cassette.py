"""Cassette layer — mechanics + a replay against a REAL captured sample.

Run: PYTHONPATH=. uv run pytest development/harness/tests/test_cassette.py
The demo cassette (development/harness/cassettes/demo.jsonl) is a verbatim recording captured
from the real ddgs/local-extractor/local-LLM via `python -m harness.capture`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from disco.core.events import LLMMessage
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole
from disco.core.llm.types import (
    CompletionResponse,
    ProposedToolCall,
    StreamChunk,
    TokenUsage,
)

from harness.cassette import Cassette, CassetteMiss, cassette_key
from harness.providers import ReplayExtractionProvider, ReplaySearchProvider
from harness.router import ReplayRouter, _req_payload

_DEMO = Path(__file__).resolve().parents[1] / "cassettes" / "demo.jsonl"


def test_key_is_stable_and_excludes_request_id():
    def req(rid):
        return CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
            messages=[LLMMessage(role="user", content="hi")],
            tools=[],
            temperature=0.0,
            request_id=rid,
        )

    # two calls differing ONLY in request_id must hash to the same cassette key —
    # otherwise replay would never match (the request_id is fresh every call).
    assert cassette_key("llm.complete", _req_payload(req("a"))) == cassette_key(
        "llm.complete", _req_payload(req("b"))
    )
    base = req("a")
    base_key = cassette_key("llm.complete", _req_payload(base))
    semantic_variants = (
        base.model_copy(update={"max_tokens": 24}),
        base.model_copy(update={"assistant_prefill": "continue"}),
        base.model_copy(update={"response_format": "json"}),
        base.model_copy(update={"assist": True}),
        base.model_copy(update={"enable_thinking": False}),
        base.model_copy(update={"attempt": 2}),
        base.model_copy(update={"provider_prefs": {"sort": "throughput"}}),
        base.model_copy(update={"stream": True}),
    )
    assert all(cassette_key("llm.complete", _req_payload(v)) != base_key for v in semantic_variants)


def test_record_lookup_and_miss():
    c = Cassette()
    c.record("search", {"query": "x", "limit": 3, "deny": []}, [{"url": "u"}])
    assert c.lookup("search", {"query": "x", "limit": 3, "deny": []}) == [{"url": "u"}]
    with pytest.raises(CassetteMiss):
        c.lookup("search", {"query": "y", "limit": 3, "deny": []})


def test_streamchunk_with_final_roundtrips():
    # the terminal stream chunk nests a CompletionResponse — verify it survives
    # model_dump(mode="json") → model_validate (the replay path's serialization).
    final = CompletionResponse(
        text="done",
        tool_calls=[ProposedToolCall(tool_name="file_write", arguments={"path": "a"})],
        usage=TokenUsage(input_tokens=1, output_tokens=2),
        finish_reason="stop",
        model_used="m",
        request_id="r",
    )
    chunk = StreamChunk(done=True, final=final)
    back = StreamChunk.model_validate(chunk.model_dump(mode="json"))
    assert back.done and back.final and back.final.text == "done"
    assert back.final.tool_calls[0].tool_name == "file_write"


@pytest.mark.skipif(not _DEMO.exists(), reason="demo cassette not captured")
async def test_replay_matches_recorded_real_sample():
    cas = Cassette.load(_DEMO)
    assert cas.seams().get("search") and cas.seams().get("extract")

    hits = await ReplaySearchProvider(cas).search("what is the vcrpy library used for", limit=3)
    assert hits and hits[0].source_engine == "ddgs"
    doc = await ReplayExtractionProvider(cas).extract(hits[0].url)
    assert doc.fetched_ok and doc.passages and doc.passages[0].source_url

    if cas.seams().get("llm.complete"):
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
            messages=[LLMMessage(role="user", content="In one sentence: what is vcrpy?")],
            tools=[],
            temperature=0.0,
            request_id="other",  # different id — replay still matches
        )
        resp = await ReplayRouter(cas).complete(req, context=None)
        assert resp.text

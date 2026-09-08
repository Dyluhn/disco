"""Cassette layer — mechanics + a replay against a REAL captured sample.

Run: uv run pytest development/harness/tests/test_cassette.py
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


async def test_search_recording_preserves_scope_recency_and_replays_without_network(tmp_path):
    from datetime import date
    from unittest.mock import AsyncMock

    from disco.retrieval.models import SearchHit
    from harness.providers import RecordingSearchProvider

    hit = SearchHit(
        url="https://example.test/a",
        title="A",
        snippet="Evidence",
        source_engine="test",
        published_at=date(2026, 9, 5),
    )
    inner = AsyncMock()
    inner.name = "test"
    inner.search.return_value = [hit]
    cassette = Cassette()
    recorder = RecordingSearchProvider(inner, cassette)
    scope = {
        "limit": 3,
        "domains_allow": frozenset({"b.test", "a.test"}),
        "domains_deny": frozenset({"blocked.test"}),
        "time_filter": "week",
    }
    assert await recorder.search("query", **scope) == [hit]
    inner.search.assert_awaited_once_with("query", **scope)
    cassette.save(tmp_path / "dated-search.jsonl")
    replay = ReplaySearchProvider(Cassette.load(tmp_path / "dated-search.jsonl"))
    assert replay.name == recorder.name == "test"
    assert await replay.search("query", **scope) == [hit]
    for changed in (
        {"domains_allow": frozenset({"c.test"})},
        {"time_filter": "month"},
        {"domains_deny": frozenset()},
        {"limit": 4},
    ):
        with pytest.raises(CassetteMiss):
            await replay.search("query", **{**scope, **changed})
    assert inner.search.await_count == 1


async def test_recorded_extraction_retries_and_redirects_preserve_each_response(tmp_path):
    from unittest.mock import AsyncMock

    from disco.retrieval.models import ExtractedDoc
    from harness.providers import RecordingExtractionProvider

    url = "https://example.test/original"
    failed = ExtractedDoc(
        url=url, title="Unavailable", content="", fetched_ok=False, error="HTTP 500", status="error"
    )
    recovered = ExtractedDoc(
        url="https://example.test/redirected",
        title="Recovered",
        content="Recovered evidence",
        fetched_ok=True,
    )
    inner = AsyncMock()
    inner.extract_many.side_effect = [[failed], [recovered]]
    cassette = Cassette()
    recording = RecordingExtractionProvider(inner, cassette)
    assert await recording.extract_many([url]) == [failed]
    assert await recording.extract_many([url]) == [recovered]
    cassette.save(tmp_path / "retries.jsonl")
    replay = ReplayExtractionProvider(Cassette.load(tmp_path / "retries.jsonl"))
    assert await replay.extract_many([url]) == [failed]
    assert await replay.extract_many([url]) == [recovered]
    with pytest.raises(CassetteMiss, match="exhausted"):
        await replay.extract_many([url])
    assert inner.extract_many.await_count == 2


def test_ordered_cassette_share_rows_roundtrip_and_reject_corrupt_order():
    cassette = Cassette()
    cassette.record_next("search", {"query": "x"}, ["first"])
    cassette.record_next("search", {"query": "x"}, ["second"])
    assert (
        len(
            Cassette.from_rows(
                [{key: value for key, value in cassette._log[0].items() if key != "output"}]
            )
        )
        == 0
    )
    replay = Cassette.from_rows(cassette._log)
    assert replay.lookup_next("search", {"query": "x"}) == ["first"]
    assert replay.lookup_next("search", {"query": "x"}) == ["second"]
    with pytest.raises(CassetteMiss, match="exhausted"):
        replay.lookup_next("search", {"query": "x"})
    with pytest.raises(ValueError, match="occurrence"):
        Cassette.from_rows(list(reversed(cassette._log)))

"""Declared reader capacity is recorded, never inferred from replay config."""

from __future__ import annotations

import json

import pytest
from disco.core.llm import ModelRole
from disco.core.llm.types import CompletionResponse, TokenUsage
from disco.retrieval.deep_research._source_reading import read_sources
from disco.retrieval.models import Passage
from harness.cassette import Cassette, CassetteMiss
from harness.router import RecordingRouter, ReplayRouter

SEAM = "llm.output_token_limit"


class CapacityRouter:
    """Controlled test service; real recorder dispatch must call it exactly once."""

    def __init__(self, capacity):
        self.capacity = capacity
        self.roles = []
        self.requests = []

    def output_token_limit(self, role):
        self.roles.append(role)
        return self.capacity

    async def complete(self, req, *, context=None):
        self.requests.append(req)
        return CompletionResponse(
            text=json.dumps(
                {
                    "sections": ["Evidence"],
                    "findings": [
                        {
                            "quote": (
                                "This retained source documents an observed limit "
                                "of ten concurrent readers."
                            ),
                            "statement": "The observed limit is ten concurrent readers.",
                            "conditions": "observed",
                            "kind": "result",
                        }
                    ],
                    "contrary": [],
                }
            ),
            usage=TokenUsage(input_tokens=40, output_tokens=100),
            finish_reason="stop",
            model_used="controlled-test-model",
            request_id=req.request_id,
        )


async def _read(router):
    async def emit(_kind, _payload):
        pass

    passage = Passage(
        id="capacity-source",
        source_url="https://example.test/evidence",
        source_title="Evidence",
        text=(
            "This retained source documents an observed limit of ten concurrent readers.\n"
            + "Additional retained context about the experiment.\n" * 200
        ),
    )
    return await read_sources(
        [passage],
        query="What limit was observed?",
        router=router,
        conversation_id="capacity-test",
        emit=emit,
        should_cancel=None,
    )


async def test_low_declared_capacity_records_and_strictly_replays_real_reader_request(tmp_path):
    live = CapacityRouter(1024)
    captured = Cassette()
    recorded = await _read(RecordingRouter(live, captured))
    assert live.roles == [ModelRole.RAG_ANSWERER]
    assert len(live.requests) == 1 and live.requests[0].max_tokens == 1024
    assert recorded["capacity-source"].ok
    assert len(recorded["capacity-source"].findings) == 1
    path = tmp_path / "capacity.jsonl"
    captured.save(path)

    replayed = await _read(ReplayRouter(Cassette.load(path)))
    # Latency reflects the live dispatch, not a semantic note difference.
    assert replayed["capacity-source"].model_dump(exclude={"latency_ms"}) == recorded[
        "capacity-source"
    ].model_dump(exclude={"latency_ms"})
    assert len(live.requests) == 1

    # A genuinely different declared limit changes the completion's semantic key.
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row["seam"] == SEAM:
            row["output"] = {"tokens": 2048}
    with pytest.raises(CassetteMiss, match="llm.complete"):
        await _read(ReplayRouter(Cassette.from_rows(rows)))


def test_output_capacity_preserves_ordered_reads_and_role_identity(tmp_path):
    live = CapacityRouter(800)
    cassette = Cassette()
    recording = RecordingRouter(live, cassette)
    assert recording.output_token_limit(ModelRole.RAG_ANSWERER) == 800
    live.capacity = 600
    assert recording.output_token_limit(ModelRole.RAG_ANSWERER) == 600
    live.capacity = None
    assert recording.output_token_limit(ModelRole.SUMMARIZER) is None
    path = tmp_path / "roles.jsonl"
    cassette.save(path)
    replay = ReplayRouter(Cassette.load(path))
    assert replay.output_token_limit(ModelRole.RAG_ANSWERER) == 800
    assert replay.output_token_limit(ModelRole.RAG_ANSWERER) == 600
    assert replay.output_token_limit(ModelRole.SUMMARIZER) is None
    with pytest.raises(CassetteMiss, match="exhausted"):
        replay.output_token_limit(ModelRole.RAG_ANSWERER)
    with pytest.raises(CassetteMiss):
        replay.output_token_limit(ModelRole.QUERY_REWRITER)


def test_legacy_absent_capacity_seam_remains_unspecified():
    cassette = Cassette()
    cassette.record("unrelated", {}, {"tokens": 64})
    replay = ReplayRouter(cassette)
    assert replay.output_token_limit(ModelRole.RAG_ANSWERER) is None
    assert replay.output_token_limit(ModelRole.RAG_ANSWERER) is None


def test_recording_absent_optional_capability_records_explicit_none(tmp_path):
    cassette = Cassette()
    assert RecordingRouter(object(), cassette).output_token_limit(ModelRole.RAG_ANSWERER) is None
    assert cassette.seams()[SEAM] == 1
    path = tmp_path / "absent.jsonl"
    cassette.save(path)
    replay = ReplayRouter(Cassette.load(path))
    assert replay.output_token_limit(ModelRole.RAG_ANSWERER) is None
    with pytest.raises(CassetteMiss, match="exhausted"):
        replay.output_token_limit(ModelRole.RAG_ANSWERER)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"tokens": True},
        {"tokens": 0},
        {"tokens": -1},
        {"tokens": "1024"},
        {"tokens": 1.5},
        {"tokens": []},
        {"tokens": 1024, "unknown": 1},
        None,
        1024,
    ],
)
def test_malformed_recorded_capacity_is_rejected_not_treated_as_unlimited(row):
    cassette = Cassette()
    cassette.record_next(SEAM, {"role": ModelRole.RAG_ANSWERER.value}, row)
    with pytest.raises(ValueError, match="output capacity"):
        ReplayRouter(cassette).output_token_limit(ModelRole.RAG_ANSWERER)


@pytest.mark.parametrize("value", [True, 0, -1, "1024", 1.5])
def test_recording_rejects_malformed_live_capacity_before_accepting_it(value):
    cassette = Cassette()
    with pytest.raises(ValueError, match="output capacity"):
        RecordingRouter(CapacityRouter(value), cassette).output_token_limit(ModelRole.RAG_ANSWERER)
    assert SEAM not in cassette.seams()

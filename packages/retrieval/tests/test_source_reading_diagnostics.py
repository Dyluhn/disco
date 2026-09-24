"""Budget closure retains safe failure provenance in the ordinary reader trail."""

import hashlib
import json

import pytest
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes, _PendingRead
from test_reader_output_budget import UsageRouter
from test_source_reading import _finding, _reply, _source
from test_source_reading_fairness import read, snapshot, sources


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "48000")


async def test_exhaustion_trail_distinguishes_empty_truncated_and_unattempted_after_reload():
    passages = sources(6)
    raw_response = '{"findings": [{"quote": "PRIVATE_RESPONSE_SENTINEL'
    router = UsageRouter(
        [("", "length"), (raw_response, "length"), ("", "length"), ("", "length")],
        usage="capacity",
    )
    saved = {}

    async def save(notes):
        saved.update(snapshot(notes))

    notes = await read(router, passages, checkpoint=save)
    trail = reader.source_reading_trail(notes)
    assert [request.metadata["source_id"] for request in router.requests] == ["0", "1", "2", "3"]
    assert [request.max_tokens for request in router.requests] == [12000] * 4
    assert sum(note.reader_output_tokens_charged or 0 for note in notes.values()) == 48000
    assert all(note.complete and not note.ok and not note.findings for note in notes.values())
    assert [row["calls"] for row in trail] == [1, 1, 1, 1, 0, 0]
    assert all("reader output budget exhausted" in row["error"] for row in trail)
    for index in (0, 2, 3):
        assert "reader returned no visible notes (finish_reason=length)" in trail[index]["error"]
    assert "reader output allowance exhausted before notes completed" in trail[1]["error"]
    for index in (4, 5):
        assert "source was not attempted" in trail[index]["error"]
        assert "last completed" not in trail[index]["error"]
    assert "PRIVATE_RESPONSE_SENTINEL" not in json.dumps(trail)
    assert all(passage.text not in json.dumps(trail) for passage in passages)
    assert saved["1"].reader_pending.reply == raw_response
    resumed = UsageRouter()
    restored = await read(resumed, passages, retained=saved)
    assert resumed.calls == 0
    assert reader.source_reading_trail(restored) == trail


@pytest.mark.parametrize(
    "mode,detail",
    [
        ("compact", "last completed reader reply contained no visible notes"),
        ("repair", "last completed reader reply was not usable JSON notes"),
        ("initial", "reader attempt was reserved; no completed reply was retained"),
    ],
)
async def test_exhausted_resume_never_copies_untrusted_pending_feedback(mode, detail):
    passage = sources(1)[0]
    note = SourceNotes(
        source_sha256=hashlib.sha256(passage.text.encode()).hexdigest(),
        calls=4,
        ok=False,
        reader_output_tokens_charged=48000,
        reader_pending=_PendingRead(
            chunk=1, mode=mode, error="PRIVATE_ERROR_SENTINEL", reply="PRIVATE_RESPONSE_SENTINEL"
        ),
    )
    router = UsageRouter()
    notes = await read(router, [passage], retained=snapshot({passage.id: note}))
    trail = reader.source_reading_trail(notes)
    assert router.calls == 0
    assert notes[passage.id].complete and not notes[passage.id].ok
    assert trail[0]["error"] == (
        f"reader output budget exhausted; {detail}; remaining text uses source windows"
    )
    assert "PRIVATE_" not in json.dumps(trail)


async def test_success_on_last_budget_token_keeps_validated_notes_and_clean_trail(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "12000")
    quote = "Measured capacity was 12 GW."
    passage = _source((quote + " ") * 400)
    router = UsageRouter([_reply([_finding("result", quote, "12 GW")])], usage="capacity")
    notes = await read(router, [passage])
    note = notes[passage.id]
    assert router.calls == 1 and note.reader_output_tokens_charged == 12000
    assert note.complete and note.ok and note.chunks == 1 and note.error is None
    assert note.reader_pending is None
    assert len(note.findings) == 1
    finding = note.findings[0]
    assert passage.text[finding.start : finding.end] == quote
    trail = reader.source_reading_trail(notes)
    assert trail[0]["ok"] and trail[0]["error"] is None
    assert trail[0]["findings_accepted"] == 1

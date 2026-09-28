"""Invalid reader envelopes are repaired once, never accepted as success."""

from __future__ import annotations

import hashlib
import json

import pytest
from _writer_doubles import RecordingRouter, collect_emits
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes
from disco.retrieval.deep_research._source_reading import read_sources
from test_source_reading import _reply, _source


async def _read(router, passage, **kwargs):
    _, emit = collect_emits()
    return await read_sources(
        [passage],
        query="capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=kwargs.pop("should_cancel", None),
        **kwargs,
    )


@pytest.mark.parametrize(
    "text", [json.dumps({"findings": []}), _reply([])], ids=["minimal", "full-empty"]
)
async def test_valid_empty_findings_stays_successful(text):
    passage = _source("Source evidence. " * 800)
    notes = (await _read(RecordingRouter([text]), passage))[passage.id]
    assert notes.ok and notes.complete
    assert notes.findings == [] and notes.chunks == 1
    assert notes.calls == 1


BAD_ENVELOPES = {
    "empty-object": ({}, "findings"),
    "unrelated-object": ({"hello": "world"}, "findings"),
    "findings-null": ({"findings": None}, "findings"),
    "findings-object": ({"findings": {}}, "findings"),
    "findings-string": ({"findings": "nope"}, "findings"),
    "sections-object": ({"findings": [], "sections": {}}, "sections"),
    "contrary-null": ({"findings": [], "contrary": None}, "contrary"),
}


@pytest.mark.parametrize(("bad", "field"), list(BAD_ENVELOPES.values()), ids=list(BAD_ENVELOPES))
async def test_malformed_envelope_is_reasked_then_not_ok(bad, field):
    passage = _source("Source evidence. " * 800)
    text = json.dumps(bad)
    notes = (await _read(RecordingRouter([text, text]), passage))[passage.id]
    assert notes.complete and not notes.ok
    assert notes.findings == [] and notes.chunks == 0
    assert notes.calls == 2 and notes.input_tokens == 2 and notes.output_tokens == 2
    assert not notes.compact_reading
    assert notes.error and field in notes.error.lower()


async def test_invalid_then_valid_repair_succeeds():
    passage = _source("Source evidence. " * 800)
    bad = json.dumps({})
    router = RecordingRouter([bad, _reply([])])
    notes = (await _read(router, passage))[passage.id]
    assert router.calls == 2
    assert notes.ok and notes.complete and notes.chunks == 1
    assert notes.calls == 2
    repair = router.requests[1].messages
    assert repair[-2].content == bad
    assert repair[-1].content and "findings" in repair[-1].content.lower()


async def test_no_repair_budget_ends_not_ok_with_one_charged_call(monkeypatch):
    monkeypatch.setattr(reader, "MAX_CALLS_PER_SOURCE", 1)
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([json.dumps({}), _reply([])])
    notes = (await _read(router, passage))[passage.id]
    assert router.calls == 1
    assert notes.complete and not notes.ok
    assert notes.calls == 1 and notes.chunks == 0


async def test_saved_failed_reading_is_not_replayed():
    passage = _source("Source evidence. " * 800)
    prior = SourceNotes(
        source_sha256=hashlib.sha256(passage.text.encode()).hexdigest(),
        calls=2,
        complete=True,
        ok=False,
        error="reader returned no notes",
    )
    router = RecordingRouter([_reply([])])
    notes = (await _read(router, passage, retained={passage.id: prior}))[passage.id]
    assert router.calls == 0
    assert notes.complete and not notes.ok and notes.calls == 2


async def test_bad_entries_inside_valid_array_stay_valid():
    passage = _source("Source evidence. " * 800)
    payload = json.dumps({"findings": [None, "x", {}, {"quote": "absent"}], "contrary": []})
    notes = (await _read(RecordingRouter([payload]), passage))[passage.id]
    assert notes.ok and notes.complete and notes.chunks == 1
    assert notes.findings == [] and notes.calls == 1


async def test_checkpoints_charge_attempts_without_advancing_chunks(monkeypatch):
    passage = _source("Source evidence. " * 800)
    saved: list[SourceNotes] = []

    async def checkpoint(snap):
        saved.append(snap[passage.id].model_copy(deep=True))

    router = RecordingRouter([json.dumps({}), json.dumps({})])
    original_complete = router.complete

    async def complete(request, *, context=None):
        # Every request must have its charge saved before it reaches the provider.
        assert saved[-1].calls == router.calls + 1
        assert not saved[-1].complete
        return await original_complete(request, context=context)

    monkeypatch.setattr(router, "complete", complete)
    notes = (await _read(router, passage, checkpoint=checkpoint))[passage.id]
    assert notes.complete and not notes.ok
    assert notes.calls == 2 and notes.chunks == 0
    assert all(note.chunks == 0 for note in saved)
    assert saved[-1] == notes

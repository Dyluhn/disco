"""Reader capacity reaches initial work and recovery without resetting spend."""

import hashlib

import pytest
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes
from disco.retrieval.deep_research._stop import ResearchStopped
from test_reader_output_budget import UsageRouter
from test_source_reading import _finding, _reply, _source
from test_source_reading_fairness import order, read, snapshot, sources


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "48000")


def compact_notes(count):
    return [
        _reply([_finding("result", f"Source {i} evidence.", f"Finding {i}")]) for i in range(count)
    ]


@pytest.mark.parametrize("count", [2, 5, 9])
@pytest.mark.parametrize("usage", ["capacity", 0])
async def test_costly_empty_initial_reads_leave_capacity_for_valid_compact_recovery(count, usage):
    passages = sources(count)
    router = UsageRouter([("", "length")] * count + compact_notes(count), usage=usage)
    notes = await read(router, passages)
    assert order(router) == [(str(i), "1") for i in range(count)] * 2
    assert all(0 < request.max_tokens <= 12000 for request in router.requests)
    assert sum(request.max_tokens for request in router.requests) == 48000
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == 48000
    assert sum(note.output_tokens for note in notes.values()) == (48000 if usage else 0)
    for i, passage in enumerate(passages):
        note = notes[passage.id]
        assert note.ok and note.complete and note.chunks == 1 and note.calls == 2
        assert note.compact_reading and note.reader_pending is None
        assert len(note.findings) == 1
        finding = note.findings[0]
        assert passage.text[finding.start : finding.end] == f"Source {i} evidence."
        assert "At most 8 findings" in router.prompt(count + i)


@pytest.mark.parametrize("stage", [1, 3, 4, 7])
async def test_tiny_budget_serves_initial_sources_before_recovery_without_overspending(
    monkeypatch, stage
):
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", str(stage))
    router = UsageRouter([("", "length")] * 6, usage="capacity")
    notes = await read(router, sources(3))
    first_count = min(stage, 3)
    assert order(router)[:first_count] == [(str(i), "1") for i in range(first_count)]
    assert all(request.max_tokens >= 1 for request in router.requests)
    assert sum(request.max_tokens for request in router.requests) == stage
    assert sum(note.reader_output_tokens_charged or 0 for note in notes.values()) == stage
    assert all(note.complete and not note.ok and not note.findings for note in notes.values())
    assert all(notes[str(i)].calls == 0 for i in range(first_count, 3))
    if stage > 3:
        assert router.calls > 3 and "At most 8 findings" in router.prompt(3)


@pytest.mark.parametrize("call_limit,declared,expected", [(3, None, 3), (9, 2, 2)])
async def test_low_configured_limits_clamp_initial_and_recovery_allocations(
    monkeypatch, call_limit, declared, expected
):
    monkeypatch.setenv("DISCO_RESEARCH_READER_MAX_OUTPUT_TOKENS", str(call_limit))
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "19")
    router = UsageRouter([""] * 3 + compact_notes(3), usage="capacity", limit=declared)
    notes = await read(router, sources(3))
    assert [request.max_tokens for request in router.requests] == [expected] * 6
    assert all(note.ok for note in notes.values())
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == expected * 6 <= 19


async def test_compact_recovery_precedes_later_chunk_without_consuming_its_share(monkeypatch):
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9000)
    passages = [_source("a" * 18000, "0"), _source("b" * 18000, "1")]
    router = UsageRouter([""] + [_reply([])] * 4, usage="capacity")
    notes = await read(router, passages)
    assert order(router) == [("0", "1"), ("1", "1"), ("0", "1"), ("1", "2"), ("0", "2")]
    assert [request.max_tokens for request in router.requests] == [12000] * 3 + [6000] * 2
    assert "At most 8 findings" in router.prompt(2)
    assert all(note.ok and note.chunks == 2 for note in notes.values())
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == 48000


async def test_stop_json_checkpoint_preserves_costly_initials_and_compact_recovery_share():
    passages = sources(5)
    saved, stopped = {}, False

    async def save(notes):
        nonlocal saved, stopped
        saved = snapshot(notes)
        stopped = all(
            note.reader_pending is not None and note.reader_pending.mode == "compact"
            for note in saved.values()
        )

    first = UsageRouter([""] * 5, usage="capacity")
    with pytest.raises(ResearchStopped):
        await read(first, passages, checkpoint=save, should_cancel=lambda: stopped)
    assert [request.max_tokens for request in first.requests] == [8000] * 5
    assert sum(note.reader_output_tokens_charged for note in saved.values()) == 40000
    assert all(note.calls == 1 and not note.ok and not note.complete for note in saved.values())
    resumed = UsageRouter(compact_notes(5), usage="capacity")
    notes = await read(resumed, passages, retained=saved)
    assert order(resumed) == [(str(i), "1") for i in range(5)]
    assert [request.max_tokens for request in resumed.requests] == [1600] * 5
    assert all(note.ok and note.calls == 2 and len(note.findings) == 1 for note in notes.values())
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == 48000


async def test_legacy_partial_resume_charges_missing_usage_before_allocating_new_work(monkeypatch):
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9000)
    passages = [_source("a" * 18000, "0"), _source("b" * 9000, "1")]
    prior = SourceNotes(
        source_sha256=hashlib.sha256(passages[0].text.encode()).hexdigest(),
        calls=1,
        chunks=1,
        ok=False,
    )
    router = UsageRouter([_reply([])] * 2, usage="capacity")
    notes = await read(router, passages, retained={"0": prior})
    assert order(router) == [("1", "1"), ("0", "2")]
    assert [request.max_tokens for request in router.requests] == [12000] * 2
    assert notes["0"].reader_output_tokens_charged == 24000
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == 36000
    assert all(note.ok for note in notes.values())


async def test_changed_hash_gets_initial_opportunity_despite_retained_attempt_count():
    passages = sources(2)
    prior = SourceNotes(
        source_sha256="old hash",
        calls=6,
        chunks=1,
        complete=True,
        reader_output_tokens_charged=12000,
    )
    router = UsageRouter(compact_notes(2), usage="capacity")
    notes = await read(router, passages, retained={"0": prior})
    assert order(router) == [("0", "1"), ("1", "1")]
    assert notes["0"].source_sha256 == hashlib.sha256(passages[0].text.encode()).hexdigest()
    assert notes["0"].calls == 7 and notes["0"].reader_output_tokens_charged == 24000
    assert sum(note.reader_output_tokens_charged for note in notes.values()) == 36000
    assert all(note.ok and note.chunks == 1 and len(note.findings) == 1 for note in notes.values())


async def test_reported_overrun_is_charged_exactly_and_stops_further_dispatch(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "100")
    router = UsageRouter([_reply([])], usage=101)
    notes = await read(router, sources(2))
    assert router.calls == 1 and router.requests[0].max_tokens == 33
    assert notes["0"].reader_output_tokens_charged == notes["0"].output_tokens == 101
    assert notes["0"].ok and notes["1"].calls == 0 and not notes["1"].ok
    assert "budget exhausted" in notes["1"].error

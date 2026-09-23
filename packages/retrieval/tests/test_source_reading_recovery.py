"""Reading work survives Stop/restart before the draft exists."""

import hashlib

import pytest
from _writer_doubles import RecordingRouter, collect_emits, outcome
from disco.retrieval.deep_research import _source_reading
from disco.retrieval.deep_research._source_notes import SourceNotes
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_checkpoint import WriterCheckpoint
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.writer import write_report
from test_deep_research import _FakeNLI
from test_source_reading import _reply, _source


async def test_reading_checkpoint_roundtrips_through_the_event_commit_owner(tmp_path, monkeypatch):
    from disco.retrieval.deep_research._run_checkpoint import RunCheckpoints
    from disco.retrieval.deep_research._writer_checkpoint import reading_checkpoint
    from disco.retrieval.deep_research.recovery import read_checkpoint
    from test_research_recovery import saved_checkpoint

    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    initial = saved_checkpoint()
    owner = RunCheckpoints(initial.conversation_id, initial.query, "quick", None, initial)
    events, emit = collect_emits()

    async def save(state):
        await owner.writer(state, emit)

    await owner.writing(None, emit)
    commit = reading_checkpoint(save, "input", "research", initial.bound.review_decisions)
    note = SourceNotes(source_sha256="source", chunks=1, calls=2)
    await commit({"p1": note})
    reference = events[-1][1]
    restored = read_checkpoint(initial.conversation_id, reference)
    assert restored.writer is not None and restored.writer.stage == "reading"
    assert restored.writer.source_notes["p1"].chunks == 1
    assert restored.writer.source_notes["p1"].calls == 2
    assert restored.writer.budget.used == 0


async def test_stop_after_first_chunk_resumes_at_second_chunk(monkeypatch):
    monkeypatch.setattr(_source_reading, "CHUNK_CHARS", 9_000)
    passage = _source("a" * 9_000 + "b" * 9_000)
    saved = {}
    stopped = False

    async def commit(notes):
        nonlocal saved, stopped
        saved = {
            key: SourceNotes.model_validate_json(value.model_dump_json())
            for key, value in notes.items()
        }
        stopped = saved[passage.id].chunks == 1

    _, emit = collect_emits()
    first = RecordingRouter([_reply([])])
    with pytest.raises(ResearchStopped):
        await _source_reading.read_sources(
            [passage],
            query="test",
            router=first,
            conversation_id="c1",
            emit=emit,
            should_cancel=lambda: stopped,
            checkpoint=commit,
        )
    assert first.calls == 1 and saved[passage.id].chunks == 1
    second = RecordingRouter([_reply([])])
    notes = await _source_reading.read_sources(
        [passage],
        query="test",
        router=second,
        conversation_id="c1",
        emit=emit,
        should_cancel=None,
        retained=saved,
    )
    assert second.calls == 1
    assert "part 2 of 2" in second.prompt(0)
    assert notes[passage.id].chunks == 2 and notes[passage.id].calls == 2
    assert notes[passage.id].input_tokens == 2  # no duplicate accounting


async def test_changed_source_hash_forces_reread():
    passage = _source("changed evidence. " * 800)
    _, emit = collect_emits()
    router = RecordingRouter([_reply([])])
    notes = await _source_reading.read_sources(
        [passage],
        query="test",
        router=router,
        conversation_id="c1",
        emit=emit,
        should_cancel=None,
        retained={passage.id: SourceNotes(source_sha256="stale", chunks=1, complete=True)},
    )
    assert router.calls == 1
    assert notes[passage.id].source_sha256 == hashlib.sha256(passage.text.encode()).hexdigest()


async def test_writer_commits_reading_before_any_draft_and_roundtrips():
    passage = _source("Measured evidence with reported figures. " * 400)
    saved = []
    _, emit = collect_emits()

    async def commit(state):
        saved.append(WriterCheckpoint.model_validate_json(state.model_dump_json()))

    router = RecordingRouter([_reply([]), ResearchStopped("report_draft")])
    with pytest.raises(ResearchStopped):
        await write_report(
            "test",
            outcome([passage]),
            router=router,
            nli=_FakeNLI(),
            bound=bounds_for("quick"),
            emit=emit,
            checkpoint=commit,
        )
    assert saved[-1].stage == "reading"
    assert saved[-1].source_notes[passage.id].complete
    assert saved[-1].source_notes[passage.id].chunks == 1
    resumed = RecordingRouter([ResearchStopped("report_draft")])
    with pytest.raises(ResearchStopped):
        await write_report(
            "test",
            outcome([passage]),
            router=resumed,
            nli=_FakeNLI(),
            bound=bounds_for("quick"),
            emit=emit,
            resume=saved[-1],
        )
    assert resumed.calls == 1
    assert "Take notes on one source" not in resumed.prompt(0)


async def test_interrupted_calls_do_not_reset_reader_allowance(monkeypatch):
    monkeypatch.setattr(_source_reading, "MAX_READER_CALLS", 1)
    passage = _source("Source evidence. " * 800)
    saved = {}

    async def commit(notes):
        saved.update(notes)

    _, emit = collect_emits()
    router = RecordingRouter([ResearchStopped("source_reading")])
    with pytest.raises(ResearchStopped):
        await _source_reading.read_sources(
            [passage],
            query="test",
            router=router,
            conversation_id="c1",
            emit=emit,
            should_cancel=None,
            checkpoint=commit,
        )
    assert saved[passage.id].calls == 1 and saved[passage.id].chunks == 0
    resumed = RecordingRouter()
    await _source_reading.read_sources(
        [passage],
        query="test",
        router=resumed,
        conversation_id="c1",
        emit=emit,
        should_cancel=None,
        retained=saved,
    )
    assert resumed.calls == 0


async def test_legacy_review_checkpoint_is_not_overwritten_by_reading():
    from disco.retrieval.deep_research._citation_aliases import citation_aliases
    from disco.retrieval.deep_research.writer import _writer_opening
    from test_source_reading import _checkpoint

    passage = _source("Old retained evidence. " * 800)
    saved = []

    async def commit(notes):
        saved.append(notes)

    _, emit = collect_emits()
    await _writer_opening(
        "test",
        outcome([passage]),
        _checkpoint(),
        citation_aliases([passage]),
        None,
        router=RecordingRouter([_reply([])]),
        conversation_id="c1",
        emit=emit,
        should_cancel=None,
        evidence_char_budget=220_000,
        reading_checkpoint=commit,
    )
    assert saved == []  # A saved draft/review boundary must never regress to reading.


async def test_last_chunk_saved_before_complete_marker_remains_successful(monkeypatch):
    monkeypatch.setattr(_source_reading, "MAX_READER_CALLS", 1)
    passage = _source("Source evidence. " * 800)
    saved = SourceNotes(
        source_sha256=hashlib.sha256(passage.text.encode()).hexdigest(),
        chunks=1,
        calls=1,
        complete=False,
    )
    _, emit = collect_emits()
    router = RecordingRouter()
    notes = await _source_reading.read_sources(
        [passage],
        query="test",
        router=router,
        conversation_id="c1",
        emit=emit,
        should_cancel=None,
        retained={passage.id: saved},
    )
    assert router.calls == 0
    assert notes[passage.id].complete and notes[passage.id].ok

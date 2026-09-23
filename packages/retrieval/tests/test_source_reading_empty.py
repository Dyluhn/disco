"""Empty extraction responses change the task without losing durable work."""

import hashlib

import pytest
from _writer_doubles import RecordingRouter, collect_emits
from disco.core.llm import LLMError
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes
from disco.retrieval.deep_research._stop import ResearchStopped
from test_source_reading import _finding, _reply, _source


async def read(router, passage, **kwargs):
    _, emit = collect_emits()
    return await reader.read_sources(
        [passage],
        query="capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=kwargs.pop("should_cancel", None),
        **kwargs,
    )


@pytest.mark.parametrize("empty", ["", " \n ", "<think>checking the quotes again</think>"])
async def test_empty_answer_retries_a_smaller_extraction_without_empty_assistant(empty):
    quote = "Capacity reached 12 GW in 2024 under the stated fleet scenario."
    passage = _source(quote * 200)
    router = RecordingRouter([(empty, "length"), _reply([_finding("result", quote, "12 GW")])])
    notes = (await read(router, passage))[passage.id]
    assert router.calls == 2 and notes.ok and notes.complete
    assert len(notes.findings) == 1 and notes.output_tokens == 2
    assert notes.compact_reading
    assert "At most 40 findings" in router.prompt(0)
    assert "At most 8 findings" in router.prompt(1)
    assert passage.text in router.prompt(1)  # Recovery does not truncate evidence.
    assert [m.role for m in router.requests[1].messages] == ["system", "user"]
    assert all(r.enable_thinking is False and r.response_format == "json" for r in router.requests)
    kept = notes.findings[0]
    assert passage.text[kept.start : kept.end] == kept.quote


async def test_successful_read_does_not_enter_compact_recovery():
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([_reply([])])
    notes = (await read(router, passage))[passage.id]
    assert router.calls == 1 and notes.ok and not notes.compact_reading


@pytest.mark.parametrize("reply", ["broken JSON", '{"findings": ['])
async def test_visible_malformed_answer_keeps_existing_parse_repair(reply):
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([reply, _reply([])])
    notes = (await read(router, passage))[passage.id]
    assert notes.ok and not notes.compact_reading
    assert router.requests[1].messages[-2].content == reply
    assert "not valid JSON" in router.last_message(1)


async def test_repeated_empty_answers_are_bounded_and_diagnosed():
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([("", "length"), ("", "length")])
    notes = (await read(router, passage))[passage.id]
    assert router.calls == 2 and notes.calls == 2
    assert notes.complete and not notes.ok
    assert notes.error == "reader returned no visible notes (finish_reason=length)"
    assert notes.output_tokens == 2


async def test_empty_recovery_cannot_bypass_run_allowance(monkeypatch):
    monkeypatch.setattr(reader, "MAX_READER_CALLS", 1)
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([("", "length"), _reply([])])
    notes = (await read(router, passage))[passage.id]
    assert router.calls == 1 and notes.calls == 1 and not notes.ok


async def test_provider_failure_during_recovery_retains_accounting():
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter([("", "length"), LLMError("offline")])
    notes = (await read(router, passage))[passage.id]
    assert router.calls == 2 and not notes.ok and notes.complete
    assert notes.calls == 2 and notes.output_tokens == 1
    assert "offline" in notes.error


async def test_stop_before_recovery_persists_strategy_and_charged_call():
    passage = _source("Source evidence. " * 800)
    saved = {}
    stopped = False

    async def checkpoint(notes):
        nonlocal stopped
        saved.update(
            {k: SourceNotes.model_validate_json(v.model_dump_json()) for k, v in notes.items()}
        )
        stopped = saved[passage.id].compact_reading

    first = RecordingRouter([("", "length"), _reply([])])
    with pytest.raises(ResearchStopped):
        await read(first, passage, checkpoint=checkpoint, should_cancel=lambda: stopped)
    assert first.calls == 1
    assert saved[passage.id].output_tokens == 1
    resumed = RecordingRouter([_reply([])])
    notes = (await read(resumed, passage, retained=saved))[passage.id]
    assert resumed.calls == 1 and "At most 8 findings" in resumed.prompt(0)
    assert notes.ok and notes.complete and notes.output_tokens == 2
    assert notes.calls <= reader.MAX_CALLS_PER_SOURCE


async def test_compact_recovery_preserves_completed_chunks_on_resume(monkeypatch):
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9000)
    quote = "Capacity reached 12 GW in 2024 under the stated fleet scenario."
    passage = _source(quote + "a" * (9000 - len(quote)) + "b" * 9000)
    saved = {}

    async def checkpoint(notes):
        saved.update(
            {k: SourceNotes.model_validate_json(v.model_dump_json()) for k, v in notes.items()}
        )

    first = RecordingRouter(
        [
            _reply([_finding("result", quote, "12 GW")]),
            ("", "length"),
            ResearchStopped("source_reading"),
        ]
    )
    with pytest.raises(ResearchStopped):
        await read(first, passage, checkpoint=checkpoint)
    prior = saved[passage.id]
    assert prior.chunks == 1 and prior.compact_reading and prior.calls == 3
    resumed = RecordingRouter([_reply([])])
    final = (await read(resumed, passage, retained=saved))[passage.id]
    assert resumed.calls == 1 and "part 2 of 2" in resumed.prompt(0)
    assert "At most 8 findings" in resumed.prompt(0)
    assert final.findings == prior.findings and final.chunks == 2
    assert final.calls == 4 and final.input_tokens == 3 and final.ok


async def test_legacy_completed_notes_remain_byte_identical():
    passage = _source("Source evidence. " * 800)
    prior = SourceNotes.model_validate(
        {
            "source_sha256": hashlib.sha256(passage.text.encode()).hexdigest(),
            "chunks": 1,
            "calls": 1,
            "complete": True,
        }
    )
    router = RecordingRouter()
    notes = (await read(router, passage, retained={passage.id: prior}))[passage.id]
    assert router.calls == 0 and notes.model_dump_json() == prior.model_dump_json()


async def test_compact_recovery_still_rejects_invented_quotes():
    passage = _source("Source evidence. " * 800)
    router = RecordingRouter(
        [
            ("", "length"),
            _reply(
                [
                    _finding(
                        "result",
                        "This quotation does not occur in the supplied source.",
                        "invented",
                    )
                ]
            ),
        ]
    )
    notes = (await read(router, passage))[passage.id]
    assert notes.compact_reading and notes.findings == [] and notes.findings_rejected == 1


async def test_resumed_compact_empty_answer_is_not_repeated_again():
    passage = _source("Source evidence. " * 800)
    prior = SourceNotes(
        source_sha256=hashlib.sha256(passage.text.encode()).hexdigest(),
        calls=1,
        compact_reading=True,
    )
    router = RecordingRouter([("", "length"), _reply([])])
    notes = (await read(router, passage, retained={passage.id: prior}))[passage.id]
    assert router.calls == 1 and notes.calls == 2 and notes.complete and not notes.ok

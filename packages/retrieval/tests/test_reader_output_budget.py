"""Reader output limits bound real request envelopes without fabricating coverage."""

import asyncio

import pytest
from _writer_doubles import RecordingRouter, collect_emits, outcome
from disco.core.llm import DefaultLLMRouter, ModelEntry, RouterConfig
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes, notes_blocks
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.writer import write_report
from test_deep_research import _FakeNLI
from test_source_reading import _REPORT, _finding, _reply, _source


class UsageRouter(RecordingRouter):
    def __init__(self, script=(), usage=0, limit=None):
        super().__init__(script)
        self.usage = usage
        self.limit = limit

    def output_token_limit(self, role):
        return self.limit

    async def complete(self, request, **kwargs):
        response = await super().complete(request, **kwargs)
        used = request.max_tokens if self.usage == "capacity" else self.usage
        return response.model_copy(
            update={"usage": response.usage.model_copy(update={"output_tokens": used})}
        )


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_MAX_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "150")


async def read(router, passages, **kwargs):
    _, emit = collect_emits()
    return await reader.read_sources(
        passages,
        query="capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
        **kwargs,
    )


async def test_truncated_parseable_notes_cannot_pass_and_retry_spends_same_budget():
    passage = _source("Measured capacity was 12 GW. " * 400)
    truncated = _reply([_finding("result", "Measured capacity was 12 GW.", "12 GW")])
    router = UsageRouter([(truncated, "length"), (truncated, "length")], usage="capacity")
    note = (await read(router, [passage]))[passage.id]
    assert [r.max_tokens for r in router.requests] == [75, 75]
    assert note.reader_output_tokens_charged == 150
    assert not note.ok and note.chunks == 0 and not note.findings
    assert "allowance exhausted" in note.error


async def test_usage_missing_retains_reservation_and_later_source_is_honestly_unread():
    # Retained historical ID: missing usage must no longer starve the third
    # source when capacity permits its initial opportunity.
    sources = [_source("Source evidence. " * 800, str(i)) for i in range(3)]
    router = UsageRouter([_reply([])] * 3, usage=0)
    notes = await read(router, sources)
    assert [r.max_tokens for r in router.requests] == [37, 37, 38]
    assert [notes[str(i)].reader_output_tokens_charged for i in range(3)] == [37, 37, 38]
    assert all(n.ok and n.chunks == 1 and n.output_tokens == 0 for n in notes.values())
    assert all(source.text == "Source evidence. " * 800 for source in sources)


async def test_model_declared_capacity_clamps_every_reader_request_and_known_usage_releases():
    passage = _source("Source evidence. " * 800)
    router = UsageRouter([("", "length"), _reply([])], usage=20, limit=40)
    note = (await read(router, [passage]))[passage.id]
    assert [r.max_tokens for r in router.requests] == [40, 40]
    assert note.reader_output_tokens_charged == 40 and note.ok
    assert all(r.enable_thinking is False for r in router.requests)  # intent only


async def test_cancelled_dispatch_reservation_survives_json_checkpoint_and_resume():
    passage = _source("Source evidence. " * 800)
    saved = {}

    async def save(notes):
        saved.update(
            {k: SourceNotes.model_validate_json(v.model_dump_json()) for k, v in notes.items()}
        )

    interrupted = UsageRouter([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await read(interrupted, [passage], checkpoint=save)
    assert saved[passage.id].reader_output_tokens_charged == 75
    resumed = UsageRouter([_reply([])], usage="capacity")
    note = (await read(resumed, [passage], retained=saved))[passage.id]
    assert resumed.requests[0].max_tokens == 75
    assert note.reader_output_tokens_charged == 150 and note.ok


async def test_partial_chunk_notes_and_offsets_survive_exhaustion(monkeypatch):
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9000)
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "100")
    quote = "Measured capacity was 12 GW."
    text = quote + "a" * (9000 - len(quote)) + "b" * 9000
    passage = _source(text)
    router = UsageRouter(
        [_reply([_finding("result", quote, "12 GW")]), ("", "length")], usage="capacity"
    )
    note = (await read(router, [passage]))[passage.id]
    assert router.calls == 2 and note.chunks == 1 and not note.ok
    assert [r.max_tokens for r in router.requests] == [50, 50]
    assert note.reader_output_tokens_charged == 100
    finding = note.findings[0]
    assert text[finding.start : finding.end] == quote
    assert quote in notes_blocks({passage.id: note})[passage.id]
    assert passage.text == text
    rerun = UsageRouter([_reply([])])
    await read(rerun, [passage], retained={passage.id: note})
    assert rerun.calls == 0


async def test_changed_source_requires_fresh_proof_without_refunding_prior_output():
    passage = _source("Source evidence. " * 800)
    prior = SourceNotes(
        source_sha256="old", chunks=1, complete=True, reader_output_tokens_charged=100
    )
    router = UsageRouter([_reply([])], usage="capacity")
    note = (await read(router, [passage], retained={passage.id: prior}))[passage.id]
    assert router.requests[0].max_tokens == 25
    assert note.reader_output_tokens_charged == 125
    assert note.source_sha256 != "old"


async def test_incomplete_read_is_disclosed_to_writer_and_finished_report(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "100")
    passage = _source("Measured evidence with reported figures. " * 400)
    router = UsageRouter(
        [("", "length"), ("", "length"), _REPORT, '{"passes": true, "failures": []}'],
        usage="capacity",
    )
    _, emit = collect_emits()
    written = await write_report(
        "capacity",
        outcome([passage]),
        router=router,
        nli=_FakeNLI(),
        bound=bounds_for("quick"),
        emit=emit,
        conversation_id="reader-budget",
    )
    draft = next(req for req in router.requests if req.metadata["inspect_stage"] == "report_draft")
    assert "Full source reading was incomplete" in draft.messages[-1].content
    assert any("Full source reading was incomplete" in note for note in written.review_notes)


@pytest.mark.parametrize("identity", ["arbitrary-model", "gpt-4o", "qwen3"])
def test_output_limit_is_declared_config_not_model_identity(identity):
    entry = ModelEntry(
        model_id=identity, provider="endpoint", context_window=10000, max_output_tokens=321
    )
    config = RouterConfig(models={"m": entry}, default_model="m")
    router = DefaultLLMRouter(config, {})
    from disco.core.llm import ModelRole

    assert router.output_token_limit(ModelRole.RAG_ANSWERER) == 321
    config.models["m"] = entry.model_copy(update={"max_output_tokens": None})
    assert router.output_token_limit(ModelRole.RAG_ANSWERER) is None


@pytest.mark.parametrize("replacement", [[], [_source("Now a short source.")]])
async def test_removed_or_shortened_source_spend_survives_another_checkpoint(replacement):
    old = SourceNotes(source_sha256="old", calls=1, complete=True, reader_output_tokens_charged=100)
    saved = {}

    async def save(notes):
        saved.update(
            {k: SourceNotes.model_validate_json(v.model_dump_json()) for k, v in notes.items()}
        )

    router = UsageRouter()
    notes = await read(router, replacement, retained={"p1": old}, checkpoint=save)
    assert router.calls == 0
    assert saved["p1"].reader_output_tokens_charged == 100
    assert not saved["p1"].source_sha256 and not notes_blocks(notes)
    assert reader.source_reading_trail(notes) == []
    later = UsageRouter([_reply([])], usage="capacity")
    note = (await read(later, [_source("Source evidence. " * 800)], retained=saved))["p1"]
    assert later.requests[0].max_tokens == 25
    assert note.reader_output_tokens_charged == 125

"""Durable scheduling distributes bounded reader work without inventing coverage."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from _writer_doubles import collect_emits
from disco.core.llm import LLMError
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._source_notes import SourceNotes
from disco.retrieval.deep_research._stop import ResearchStopped
from test_reader_output_budget import UsageRouter
from test_source_reading import _finding, _reply, _source


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    monkeypatch.setenv("DISCO_RESEARCH_READER_MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setenv("DISCO_RESEARCH_READER_OUTPUT_BUDGET_TOKENS", "48000")


def sources(count=3):
    return [_source(f"Source {i} evidence. " * 800, str(i)) for i in range(count)]


async def read(router, passages, **kwargs):
    _, emit = collect_emits()
    return await reader.read_sources(
        passages,
        query="evidence",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=kwargs.pop("should_cancel", None),
        **kwargs,
    )


def order(router):
    return [(r.metadata["source_id"], r.metadata["chunk"]) for r in router.requests]


def snapshot(notes):
    return {
        key: SourceNotes.model_validate_json(note.model_dump_json()) for key, note in notes.items()
    }


async def test_six_empty_sources_receive_four_first_attempts_not_two_immediate_retries():
    router = UsageRouter([("", "length")] * 4, usage="capacity")
    notes = await read(router, sources(6))
    assert order(router) == [(str(i), "1") for i in range(4)]
    assert [r.max_tokens for r in router.requests] == [12000] * 4
    assert sum(n.reader_output_tokens_charged or 0 for n in notes.values()) == 48000
    assert all(n.complete and not n.ok and n.chunks == 0 for n in notes.values())
    assert [notes[str(i)].calls for i in range(6)] == [1, 1, 1, 1, 0, 0]
    assert all("budget exhausted" in n.error for n in notes.values())


async def test_released_capacity_recovers_after_peers_with_exact_source_and_feedback():
    passages = sources()
    router = UsageRouter(
        ["broken JSON", ("", "length"), _reply([]), _reply([]), _reply([])], usage=100
    )
    notes = await read(router, passages)
    assert order(router) == [("0", "1"), ("1", "1"), ("2", "1"), ("0", "1"), ("1", "1")]
    assert router.requests[3].messages[-2].content == "broken JSON"
    assert "not valid JSON" in router.requests[3].messages[-1].content
    assert passages[0].text in router.requests[3].messages[1].content
    assert passages[1].text in router.requests[4].messages[1].content
    assert [m.role for m in router.requests[4].messages] == ["system", "user"]
    assert "At most 8 findings" in router.prompt(4)
    assert all(n.ok and n.complete and n.reader_pending is None for n in notes.values())
    assert sum(n.reader_output_tokens_charged for n in notes.values()) == 500


async def test_long_sources_rotate_chunks_and_carry_only_their_own_notes(monkeypatch):
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9000)
    quote = "Unique first source finding."
    passages = [
        _source(quote + "a" * (18000 - len(quote)), "0"),
        _source("b" * 18000, "1"),
        _source("c" * 9000, "2"),
    ]
    router = UsageRouter(
        [_reply([_finding("result", quote, "first source fact")])] + [_reply([])] * 4, usage=50
    )
    notes = await read(router, passages)
    assert order(router) == [("0", "1"), ("1", "1"), ("2", "1"), ("0", "2"), ("1", "2")]
    assert "first source fact" in router.prompt(3)
    assert "first source fact" not in router.prompt(4)
    assert [notes[str(i)].chunks for i in range(3)] == [2, 2, 1]
    assert all(n.ok for n in notes.values())


@pytest.mark.parametrize("reply,mode", [("broken JSON", "repair"), ("", "compact")])
async def test_deferred_recovery_checkpoint_resumes_fresh_sources_before_retry(reply, mode):
    passages = sources(2)
    saved, stopped = {}, False

    async def save(notes):
        nonlocal saved, stopped
        saved = snapshot(notes)
        pending = saved["0"].reader_pending
        stopped = pending is not None and pending.mode == mode

    first = UsageRouter([reply], usage=100)
    with pytest.raises(ResearchStopped):
        await read(first, passages, checkpoint=save, should_cancel=lambda: stopped)
    assert first.calls == 1 and not saved["0"].complete and not saved["0"].ok
    assert saved["0"].reader_output_tokens_charged == 100
    resumed = UsageRouter([_reply([]), _reply([])], usage=100)
    notes = await read(resumed, passages, retained=saved)
    assert order(resumed) == [("1", "1"), ("0", "1")]
    if reply:
        assert resumed.requests[1].messages[-2].content == reply
    else:
        assert "At most 8 findings" in resumed.prompt(1)
    assert notes["0"].calls == 2 and all(n.ok for n in notes.values())
    assert sum(n.reader_output_tokens_charged for n in notes.values()) == 300


async def test_interrupted_first_reservation_is_charged_and_deferred_after_fresh_peer():
    passages = sources(2)
    saved = {}

    async def save(notes):
        saved.update(snapshot(notes))

    with pytest.raises(asyncio.CancelledError):
        await read(UsageRouter([asyncio.CancelledError()]), passages, checkpoint=save)
    assert saved["0"].calls == 1 and saved["0"].reader_pending.mode == "initial"
    assert saved["0"].reader_output_tokens_charged == 12000 and not saved["0"].complete
    resumed = UsageRouter([_reply([]), _reply([])], usage=100)
    notes = await read(resumed, passages, retained=saved)
    assert order(resumed) == [("1", "1"), ("0", "1")]
    assert notes["0"].calls == 2 and notes["0"].reader_output_tokens_charged == 12100
    assert sum(n.reader_output_tokens_charged for n in notes.values()) == 12200


async def test_repeated_interrupted_recovery_cannot_reset_stage_budget():
    passages = sources(1)
    saved, stopped = {}, False

    async def save(notes):
        nonlocal stopped
        saved.update(snapshot(notes))
        stopped = saved["0"].reader_pending.mode == "compact"

    with pytest.raises(ResearchStopped):
        await read(
            UsageRouter([""], usage="capacity"),
            passages,
            checkpoint=save,
            should_cancel=lambda: stopped,
        )
    for _ in range(3):
        with pytest.raises(asyncio.CancelledError):
            await read(
                UsageRouter([asyncio.CancelledError()]), passages, retained=saved, checkpoint=save
            )
    assert saved["0"].calls == 4 and saved["0"].reader_output_tokens_charged == 48000
    resumed = UsageRouter([_reply([])])
    final = await read(resumed, passages, retained=saved)
    assert resumed.calls == 0 and final["0"].complete and not final["0"].ok


async def test_missing_usage_and_declared_cap_are_preserved_across_fair_order():
    router = UsageRouter([("", "length"), _reply([]), _reply([]), _reply([])], usage=0, limit=321)
    notes = await read(router, sources())
    assert order(router) == [("0", "1"), ("1", "1"), ("2", "1"), ("0", "1")]
    assert all(r.max_tokens == 321 for r in router.requests)
    assert sum(n.reader_output_tokens_charged for n in notes.values()) == 1284


async def test_provider_failure_is_terminal_while_other_sources_continue():
    router = UsageRouter([LLMError("offline"), _reply([])], usage=100)
    notes = await read(router, sources(2))
    assert order(router) == [("0", "1"), ("1", "1")]
    assert notes["0"].complete and not notes["0"].ok and "offline" in notes["0"].error
    assert notes["0"].reader_output_tokens_charged == 12000
    assert notes["1"].ok


async def test_completed_legacy_failure_is_not_reopened_and_removed_calls_stay_spent(monkeypatch):
    monkeypatch.setattr(reader, "MAX_READER_CALLS", 2)
    passages = sources(2)
    prior = SourceNotes(
        source_sha256=hashlib.sha256(passages[0].text.encode()).hexdigest(),
        calls=1,
        complete=True,
        ok=False,
        error="old terminal failure",
    )
    removed = SourceNotes(source_sha256="gone", calls=1, complete=True)
    router = UsageRouter()
    notes = await read(router, passages, retained={"0": prior, "removed": removed})
    assert router.calls == 0 and notes["0"] == prior
    assert notes["removed"].calls == 1
    assert not notes["1"].ok and "run allowance exhausted" in notes["1"].error


async def test_stop_at_pre_dispatch_commit_keeps_charge_without_sending_request():
    passages = sources(2)
    saved = {}

    async def save(notes):
        saved.update(snapshot(notes))
        raise ResearchStopped("source_reading")

    first = UsageRouter([_reply([])], usage=100)
    with pytest.raises(ResearchStopped):
        await read(first, passages, checkpoint=save)
    assert first.calls == 0
    assert saved["0"].calls == 1 and saved["0"].reader_output_tokens_charged == 12000
    resumed = UsageRouter([_reply([]), _reply([])], usage=100)
    notes = await read(resumed, passages, retained=saved)
    assert order(resumed) == [("1", "1"), ("0", "1")]
    assert notes["0"].calls == 2 and notes["0"].reader_output_tokens_charged == 12100


async def test_deferred_malformed_feedback_is_bounded_and_reconstructed_after_restart():
    passage = sources(1)[0]
    saved, stopped = {}, False
    malformed = "invalid visible response " * 4000

    async def save(notes):
        nonlocal stopped
        saved.update(snapshot(notes))
        pending = saved["0"].reader_pending
        stopped = pending is not None and pending.mode == "repair"

    with pytest.raises(ResearchStopped):
        await read(
            UsageRouter([malformed], usage=100),
            [passage],
            checkpoint=save,
            should_cancel=lambda: stopped,
        )
    pending = saved["0"].reader_pending
    assert pending.reply == malformed[:65536] and len(pending.error) <= 2048
    resumed = UsageRouter([_reply([])], usage=100)
    await read(resumed, [passage], retained=saved)
    assert resumed.requests[0].messages[-2].content == pending.reply
    assert pending.error in resumed.requests[0].messages[-1].content
    assert passage.text in resumed.requests[0].messages[1].content


async def test_changed_source_discards_pending_feedback_but_keeps_both_budgets():
    old = sources(1)[0]
    saved, stopped = {}, False

    async def save(notes):
        nonlocal stopped
        saved.update(snapshot(notes))
        pending = saved["0"].reader_pending
        stopped = pending is not None and pending.mode == "repair"

    with pytest.raises(ResearchStopped):
        await read(
            UsageRouter(["invalid old reply"], usage=100),
            [old],
            checkpoint=save,
            should_cancel=lambda: stopped,
        )
    changed = old.model_copy(update={"text": "New source evidence. " * 800})
    resumed = UsageRouter([_reply([])], usage=100)
    final = (await read(resumed, [changed], retained=saved))["0"]
    assert [m.role for m in resumed.requests[0].messages] == ["system", "user"]
    assert changed.text in resumed.prompt(0) and "invalid old reply" not in resumed.prompt(0)
    assert final.source_sha256 == hashlib.sha256(changed.text.encode()).hexdigest()
    assert final.calls == 2 and final.reader_output_tokens_charged == 200 and final.ok

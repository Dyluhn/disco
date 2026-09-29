"""Shown-span validation for source-reading notes.

A chunk's initial or repair reply keeps a finding or contrary quote only when
the quote occurs wholly inside the text that chunk was shown; kept spans are
original Python character offsets into the source. A repeated sentence
resolves to the shown chunk's occurrence, and a quote from an unseen chunk or
straddling a chunk boundary is dropped and counted. Whitespace normalisation,
valid empty findings, and compact-retry offset mapping behave as elsewhere.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _writer_doubles import RecordingRouter, collect_emits
from disco.retrieval.deep_research import _source_reading as reader
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.models import Passage


def _source(text: str, source_id: str = "p1") -> Passage:
    return Passage(
        id=source_id,
        source_url="https://example.test/report",
        source_title="Grid storage assessment",
        text=text,
    )


def _finding(quote: str, statement: str = "note") -> dict[str, Any]:
    return {"quote": quote, "statement": statement, "conditions": "2024 fleet", "kind": "result"}


def _payload(
    *, findings: list[dict[str, Any]] | None = None, contrary: list[dict[str, Any]] | None = None
) -> str:
    return json.dumps({"sections": [], "findings": findings or [], "contrary": contrary or []})


async def _read(router: RecordingRouter, passage: Passage, **kwargs: Any):
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


async def test_initial_finding_outside_shown_chunk_is_rejected(monkeypatch) -> None:
    """Initial reply quoting only the unseen tail must not become a finding."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    tail = "The unseen tail permits only 12 GW under a future scenario."
    passage = _source("a" * 9_000 + tail + "b" * (9_000 - len(tail)))
    router = RecordingRouter([_payload(findings=[_finding(tail, "12 GW")]), _payload()])

    notes = (await _read(router, passage))[passage.id]

    assert tail not in router.prompt(0) and tail in passage.text  # fixture: tail only in chunk 2
    assert notes.findings == [] and notes.findings_rejected == 1
    assert notes.ok and notes.complete and notes.chunks == 2
    assert notes.calls == router.calls == 2
    assert notes.input_tokens == 2 and notes.output_tokens == 2


async def test_repair_contrary_outside_shown_chunk_is_rejected(monkeypatch) -> None:
    """Parse-repair reply quoting only the unseen tail must not become contrary."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    tail = "The unseen tail permits only 12 GW under a future scenario."
    passage = _source("a" * 9_000 + tail + "b" * (9_000 - len(tail)))
    router = RecordingRouter(
        ["not json at all", _payload(contrary=[{"quote": tail, "statement": "12 GW"}]), _payload()]
    )

    notes = (await _read(router, passage))[passage.id]

    assert "not valid JSON" in router.last_message(1)
    assert tail not in router.prompt(1) and tail in passage.text
    assert notes.contrary == [] and notes.findings_rejected == 1
    assert notes.ok and notes.complete and notes.chunks == 2
    assert notes.calls == router.calls == 3


async def test_repeated_quote_resolves_to_shown_occurrence_with_unicode_prefix(monkeypatch) -> None:
    """Same sentence in both chunks: the chunk-2 read keeps chunk-2 offsets."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    quote = "Capacity is 12 GW."
    first = "é🙂漢字 " + quote + "a" * (9_000 - len("é🙂漢字 " + quote))
    passage = _source(first + "β" + quote + "b" * 8_000)
    want = passage.text.index(quote, 9_000)
    assert want == 9_001 and passage.text.index(quote) == len("é🙂漢字 ")
    router = RecordingRouter([_payload(), _payload(findings=[_finding(quote, "12 GW")])])

    notes = (await _read(router, passage))[passage.id]

    assert "part 2 of 2" in router.prompt(1)
    assert len(notes.findings) == 1 and notes.findings_rejected == 0
    kept = notes.findings[0]
    assert (kept.start, kept.end) == (want, want + len(quote))  # Python chars, not bytes
    assert kept.start >= 9_000 and passage.text[kept.start : kept.end] == quote
    assert notes.ok and notes.chunks == 2 and notes.calls == router.calls == 2


async def test_whitespace_normalized_quote_within_span_keeps_original_offsets(monkeypatch) -> None:
    """Control: newline in source vs space in quote still validates inside the span."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    prefix = "Résumé érudit 🙂漢字 "
    src = "Rated output is 4.5 GW\nunder the 2030 projected scenario only."
    quote = "Rated output is 4.5 GW under the 2030 projected scenario only."
    contra = "The pilot ran eleven days only."
    passage = _source(prefix + src + " " + contra + "c" * 8_000)
    assert len(passage.text) < 9_000  # single shown span
    router = RecordingRouter(
        [
            _payload(
                findings=[_finding(quote, "4.5 GW")],
                contrary=[{"quote": contra, "statement": "eleven days"}],
            )
        ]
    )

    notes = (await _read(router, passage))[passage.id]

    assert notes.findings_rejected == 0
    kept = notes.findings[0]
    assert (kept.start, kept.end) == (len(prefix), len(prefix) + len(src))
    assert passage.text[kept.start : kept.end] == src
    held = notes.contrary[0]
    assert passage.text[held.start : held.end] == contra
    assert notes.ok and notes.chunks == 1 and notes.calls == router.calls == 1


async def test_quote_crossing_chunk_boundary_is_rejected(monkeypatch) -> None:
    """A quote straddling 9000 is wholly shown in neither chunk: reject it."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    quote = "Capacity is 12 GW only under the future scenario."
    passage = _source("a" * 8_990 + quote + "b" * 8_000)
    assert 8_990 < 9_000 < 8_990 + len(quote)  # fixture genuinely straddles the boundary
    router = RecordingRouter([_payload(findings=[_finding(quote, "12 GW")]), _payload()])

    notes = (await _read(router, passage))[passage.id]

    assert quote not in router.prompt(0)  # first span holds only its head
    assert notes.findings == [] and notes.findings_rejected == 1
    assert notes.ok and notes.complete and notes.chunks == 2
    assert notes.calls == router.calls == 2


async def test_compact_retry_accepts_within_span_quote_with_original_offsets(monkeypatch) -> None:
    """Compact path keeps its own semantics; a within-span quote maps to source offsets."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    prefix = "État brut 🙂漢字 "
    quote = "Capacity reached 12 GW in 2024 under the stated fleet scenario."
    passage = _source(prefix + quote + "d" * 8_000)
    assert len(passage.text) < 9_000
    router = RecordingRouter([("", "length"), _payload(findings=[_finding(quote, "12 GW")])])

    notes = (await _read(router, passage))[passage.id]

    assert "At most 40 findings" in router.prompt(0)
    assert "At most 8 findings" in router.prompt(1)
    assert notes.compact_reading and notes.ok and notes.complete
    assert len(notes.findings) == 1 and notes.findings_rejected == 0
    kept = notes.findings[0]
    assert (kept.start, kept.end) == (len(prefix), len(prefix) + len(quote))
    assert passage.text[kept.start : kept.end] == quote
    assert notes.chunks == 1 and notes.calls == router.calls == 2


async def test_identical_quotes_in_distinct_chunks_keep_distinct_conditions(monkeypatch) -> None:
    """Identical wording under different conditions remains separate evidence."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    quote = "Capacity reached 12 GW."
    first = "Northern fleet: " + quote
    passage = _source(first + "a" * (9_000 - len(first)) + "Southern fleet: " + quote + "b" * 8_000)
    north = {**_finding(quote, "Northern capacity"), "conditions": "northern fleet"}
    south = {**_finding(quote, "Southern capacity"), "conditions": "southern fleet"}
    router = RecordingRouter([_payload(findings=[north]), _payload(findings=[south])])
    notes = (await _read(router, passage))[passage.id]
    assert len(notes.findings) == 2
    assert [f.conditions for f in notes.findings] == ["northern fleet", "southern fleet"]
    assert [f.start for f in notes.findings] == [
        passage.text.index(quote),
        passage.text.index(quote, 9_000),
    ]
    assert notes.ok and notes.chunks == 2 and notes.calls == router.calls == 2


async def test_resume_keeps_prior_offsets_and_shifts_new_finding_and_contrary(monkeypatch) -> None:
    """Saved first-chunk notes survive a real stop and second-chunk resume."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    first_quote = "First observed capacity reached 12 GW."
    second_quote = "Later observed capacity reached 15 GW."
    contrary = "The later pilot ran eleven days only."
    prefix = "Étude 🙂漢字 "
    first_chunk = prefix + first_quote
    first_chunk += "a" * (9_000 - len(first_chunk))
    passage = _source(first_chunk + "β " + second_quote + " " + contrary + "b" * 8_000)
    saved = {}
    stopped = False

    async def checkpoint(notes):
        nonlocal saved, stopped
        saved = {
            key: reader.SourceNotes.model_validate_json(value.model_dump_json())
            for key, value in notes.items()
        }
        stopped = saved[passage.id].chunks == 1

    first = RecordingRouter([_payload(findings=[_finding(first_quote)])])
    with pytest.raises(ResearchStopped):
        await _read(first, passage, checkpoint=checkpoint, should_cancel=lambda: stopped)
    prior = saved[passage.id].findings[0]
    prior_span = (prior.start, prior.end)
    assert prior_span == (len(prefix), len(prefix) + len(first_quote))
    second = RecordingRouter(
        [
            _payload(
                findings=[_finding(second_quote)],
                contrary=[{"quote": contrary, "statement": "eleven days"}],
            )
        ]
    )
    notes = (await _read(second, passage, retained=saved))[passage.id]
    assert first.calls == second.calls == 1
    assert "part 2 of 2" in second.prompt(0)
    assert (notes.findings[0].start, notes.findings[0].end) == prior_span
    assert notes.findings[1].start == passage.text.index(second_quote, 9_000)
    assert notes.contrary[0].start == passage.text.index(contrary, 9_000)
    for kept in [*notes.findings, *notes.contrary]:
        assert passage.text[kept.start : kept.end] == kept.quote
    assert notes.ok and notes.complete and notes.chunks == notes.calls == 2
    assert notes.input_tokens == notes.output_tokens == 2


async def test_quotes_touching_either_side_of_chunk_boundary_are_valid(monkeypatch) -> None:
    """Ending or starting exactly at a boundary is different from crossing it."""
    monkeypatch.setattr(reader, "CHUNK_CHARS", 9_000)
    left = "Northern capacity reached 12 GW."
    right = "Southern capacity reached 15 GW."
    passage = _source("a" * (9_000 - len(left)) + left + right + "b" * 8_000)
    router = RecordingRouter(
        [_payload(findings=[_finding(left)]), _payload(findings=[_finding(right)])]
    )
    notes = (await _read(router, passage))[passage.id]
    assert [(f.start, f.end) for f in notes.findings] == [
        (9_000 - len(left), 9_000),
        (9_000, 9_000 + len(right)),
    ]
    assert notes.findings_rejected == 0 and notes.ok and notes.chunks == 2

"""Reading each admitted source in full, once, and proving the notes back.

The windowing the writer used to see is keyword-selected: on a 166K-character
source it shows five to twenty percent of the text, and the sentence that
carries the decisive number is often not in it. This stage reads the whole
source in its own call and hands the writer notes instead — but a note is only
evidence if its quote is really in the source, so every quote is located in the
text before the writer ever sees it, and the offsets it is shown are exact.

Reader failure is never the run's failure: a source whose notes do not parse
falls back to exactly the windows it had before, and says so in the trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from _writer_doubles import RecordingRouter, collect_emits, outcome, report_markdown
from disco.retrieval.deep_research import _source_reading
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._source_reading import (
    CHUNK_CHARS,
    NOTES_HEADER,
    SourceNotes,
    notes_blocks,
    notes_from_payload,
    read_sources,
    render_source_notes,
    source_reading_trail,
)
from disco.retrieval.deep_research._writer_checkpoint import WriterCheckpoint
from disco.retrieval.deep_research._writer_evidence import format_evidence_pool
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.writer import write_report
from disco.retrieval.models import Passage
from test_deep_research import _FakeNLI


def await_sync(coroutine):
    """Run one coroutine to completion from a synchronous test."""
    return asyncio.run(coroutine)


def _source(text: str, source_id: str = "p1") -> Passage:
    return Passage(
        id=source_id,
        source_url="https://example.test/report",
        source_title="Grid storage assessment",
        text=text,
    )


def _reply(findings: list[dict[str, Any]], **extra: Any) -> str:
    return json.dumps({"sections": ["Results"], "findings": findings, "contrary": [], **extra})


# ---------------------------------------------------------------------------
# The validation wall.
# ---------------------------------------------------------------------------


def test_validation_drops_an_unquotable_finding_and_offsets_the_real_one() -> None:
    text = (
        "Introduction to the assessment.\n\n"
        "The plant delivered 320 MWh over a five-hour discharge duration.\n\n"
        "Closing remarks."
    )
    payload = {
        "sections": ["Introduction", "Results"],
        "findings": [
            {
                "quote": "The plant delivered 320 MWh over a five-hour discharge duration.",
                "statement": "The plant delivered 320 MWh",
                "conditions": "five-hour discharge duration",
                "kind": "result",
            },
            {
                "quote": "The plant delivered 900 MWh over a five-hour discharge duration.",
                "statement": "The plant delivered 900 MWh",
                "conditions": "five-hour discharge duration",
                "kind": "result",
            },
        ],
        "contrary": [],
    }

    notes = notes_from_payload(payload, text)

    assert [finding.statement for finding in notes.findings] == ["The plant delivered 320 MWh"]
    assert notes.findings_rejected == 1
    kept = notes.findings[0]
    assert text[kept.start : kept.end] == kept.quote
    assert notes.sections == ["Introduction", "Results"]


def test_a_quote_broken_across_lines_still_validates_to_its_exact_offsets() -> None:
    text = "Rated output is 4.5 GW\nunder the 2030 projected scenario only."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "Rated output is 4.5 GW under the 2030 projected scenario only.",
                "statement": "Rated output is 4.5 GW",
                "conditions": "2030 projected scenario",
                "kind": "projection",
            }
        ],
        "contrary": [],
    }

    notes = notes_from_payload(payload, text)

    assert notes.findings_rejected == 0
    assert (notes.findings[0].start, notes.findings[0].end) == (0, len(text))


# ---------------------------------------------------------------------------
# One call per source, chunked only when the source is longer than one call.
# ---------------------------------------------------------------------------


async def test_a_source_longer_than_one_chunk_is_read_in_two_and_the_notes_merge() -> None:
    head = "Head marker: capacity reached 12 GW in 2024. "
    tail = "Tail marker: the pilot ran for eleven days only. "
    text = head + "filler sentence. " * ((CHUNK_CHARS - len(head)) // 17) + tail
    text += "x" * (2 * CHUNK_CHARS - len(text) - 100)
    passage = _source(text)
    assert len(text) > CHUNK_CHARS
    router = RecordingRouter(
        [
            _reply(
                [
                    {
                        "quote": "Head marker: capacity reached 12 GW in 2024.",
                        "statement": "Capacity reached 12 GW",
                        "conditions": "2024",
                        "kind": "result",
                    }
                ]
            ),
            _reply(
                [
                    {
                        "quote": "Tail marker: the pilot ran for eleven days only.",
                        "statement": "The pilot ran eleven days",
                        "conditions": "pilot",
                        "kind": "limitation",
                    }
                ]
            ),
        ]
    )
    _captured, emit = collect_emits()

    notes = await read_sources(
        [passage],
        query="grid capacity",
        router=router,
        conversation_id="conv_reader",
        emit=emit,
        should_cancel=None,
    )

    assert router.stages == ["source_reading", "source_reading"]
    assert [statement.statement for statement in notes["p1"].findings] == [
        "Capacity reached 12 GW",
        "The pilot ran eleven days",
    ]
    assert notes["p1"].chunks == 2
    assert notes["p1"].ok is True
    assert "Capacity reached 12 GW" in router.prompt(1)


async def test_a_short_source_is_never_read_because_the_writer_sees_it_whole() -> None:
    router = RecordingRouter([])
    _captured, emit = collect_emits()

    notes = await read_sources(
        [_source("short source text. " * 10)],
        query="grid capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert notes == {}
    assert router.calls == 0


# ---------------------------------------------------------------------------
# Failure degrades to the windows the writer already had.
# ---------------------------------------------------------------------------


async def test_a_parse_failure_is_reasked_once_then_degrades_to_windows() -> None:
    passage = _source("Capacity reached 12 GW in 2024. " * 400)
    router = RecordingRouter(["not json at all", "still not json"])
    captured, emit = collect_emits()

    notes = await read_sources(
        [passage],
        query="grid capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert router.calls == 2
    assert "not valid JSON" in router.last_message(1)
    assert notes["p1"].findings == []
    assert notes["p1"].ok is False
    row = source_reading_trail(notes)[0]
    assert row["kind"] == "source_reading"
    assert row["source_id"] == "p1"
    assert row["source_sha256"] == hashlib.sha256(passage.text.encode()).hexdigest()
    assert row["ok"] is False
    assert "not valid JSON" in row["error"]
    assert notes_blocks(notes) == {}
    assert ("phase", {"phase": "reading", "source": 1, "of": 1}) in captured


async def test_a_provider_failure_leaves_the_source_with_windows_and_one_call() -> None:
    from disco.core.llm import LLMError

    passage = _source("Capacity reached 12 GW in 2024. " * 400)
    router = RecordingRouter([LLMError("provider is down")])
    _captured, emit = collect_emits()

    notes = await read_sources(
        [passage],
        query="grid capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert router.calls == 1
    assert notes["p1"].ok is False
    assert notes_blocks(notes) == {}


async def test_the_run_call_bound_stops_reading_further_sources(monkeypatch) -> None:
    monkeypatch.setattr(_source_reading, "MAX_READER_CALLS", 1)
    passages = [_source("Capacity reached 12 GW in 2024. " * 400, f"p{index}") for index in (1, 2)]
    router = RecordingRouter([_reply([])])
    _captured, emit = collect_emits()

    notes = await read_sources(
        passages,
        query="grid capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert router.calls == 1
    assert list(notes) == ["p1"]


# ---------------------------------------------------------------------------
# Rendering: notes first, priority cap, inside the source's own share.
# ---------------------------------------------------------------------------


def _finding(kind: str, quote: str, statement: str) -> dict[str, Any]:
    return {"quote": quote, "statement": statement, "conditions": "2024 fleet", "kind": kind}


def test_every_accepted_finding_is_rendered_when_the_share_allows() -> None:
    # The 6,000-character per-source cap discarded 61% of S2's accepted
    # findings — 172 of 286 — including the 42% outflow result the report
    # needed. With room, nothing accepted is withheld.
    text = " ".join(f"Finding number {index} measured 7 units." for index in range(34))
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": f"Finding number {index} measured 7 units.",
                "statement": f"finding {index}",
                "conditions": "2024",
                "kind": "result",
            }
            for index in range(34)
        ],
        "contrary": [],
    }
    notes = notes_from_payload(payload, text)
    assert len(notes.findings) == 34

    block = render_source_notes(notes)

    assert "finding 33" in block
    assert all(f"finding {index}" in block for index in range(34))


def test_the_share_drops_the_lowest_priority_notes_first() -> None:
    text = "Alpha result sentence. Bravo claim sentence. Charlie table row 9 GW. "
    payload = {
        "sections": [],
        "findings": [
            _finding("result", "Alpha result sentence.", "alpha"),
            _finding("claim", "Bravo claim sentence.", "bravo"),
            _finding("table", "Charlie table row 9 GW.", "charlie"),
        ],
        "contrary": [],
    }
    notes = notes_from_payload(payload, text)

    block = render_source_notes(notes, char_cap=len(NOTES_HEADER) + 200)

    assert "alpha" in block
    assert "charlie" in block
    assert "bravo" not in block


def test_notes_are_ordered_so_the_weakest_is_the_one_a_tight_share_loses() -> None:
    text = "A result here. A method here. A contrary note here. A claim here. "
    payload = {
        "sections": [],
        "findings": [
            _finding("method", "A method here.", "method-note"),
            _finding("claim", "A claim here.", "claim-note"),
            _finding("result", "A result here.", "result-note"),
        ],
        "contrary": [{"quote": "A contrary note here.", "statement": "contrary-note"}],
    }

    block = render_source_notes(notes_from_payload(payload, text))
    order = [
        block.index("result-note"),
        block.index("contrary-note"),
        block.index("claim-note"),
        block.index("method-note"),
    ]

    assert order == sorted(order)


def test_a_rendered_finding_carries_its_offsets_quote_statement_and_condition() -> None:
    text = "The fleet stored 26 GW of capacity across the 2024 reporting year."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "The fleet stored 26 GW of capacity",
                "statement": "The fleet stored 26 GW",
                "conditions": "2024 reporting year, United States fleet",
                "kind": "result",
            }
        ],
        "contrary": [
            {
                "quote": "across the 2024 reporting year",
                "statement": "The window is one year",
            }
        ],
    }

    block = render_source_notes(notes_from_payload(payload, text))

    assert block == (
        "NOTES (validated quotes; offsets are exact)\n"
        '- [chars 0:34] "The fleet stored 26 GW of capacity" — The fleet stored 26 GW. '
        "Condition: 2024 reporting year, United States fleet.\n"
        '- [chars 35:65] "across the 2024 reporting year" — '
        "Contrary to the source's own headline: The window is one year."
    )


def test_notes_render_before_the_windows_and_inside_the_source_share() -> None:
    passage = _source("Capacity reached 12 GW in the 2024 fleet. " * 300)
    block = 'NOTES (validated quotes; offsets are exact)\n- [chars 0:10] "Capacity r" — a. C: b.'

    rendered = format_evidence_pool(
        [passage], char_budget=1200, query="capacity", notes={passage.id: block}
    )

    body = rendered.split("\n", 2)[2]
    assert body.startswith(block)
    assert "Capacity reached 12 GW" in body[len(block) :]
    assert len(rendered) <= 1200


def test_windows_appear_only_after_the_notes_and_only_with_share_left_over() -> None:
    passage = _source("Capacity reached 12 GW in the 2024 fleet. " * 300)
    header_chars = len(f"[p1] {passage.source_title}\n{passage.source_url}\n")
    block = "\n".join([NOTES_HEADER, *[f"- note line {index}" for index in range(40)]])

    rendered = format_evidence_pool(
        [passage],
        char_budget=header_chars + len(block),
        query="capacity",
        notes={passage.id: block},
    )

    body = rendered.split("\n", 2)[2]
    assert body == block
    assert "[source characters" not in body


def test_a_notes_block_larger_than_the_share_drops_whole_lines() -> None:
    passage = _source("Capacity reached 12 GW in the 2024 fleet. " * 300)
    block = "\n".join([NOTES_HEADER, "- first note line", "- second note line " + "x" * 900])

    rendered = format_evidence_pool(
        [passage], char_budget=400, query="capacity", notes={passage.id: block}
    )

    assert "- first note line" in rendered
    assert "second note line" not in rendered
    assert len(rendered) <= 400


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def _checkpoint(**extra: Any) -> WriterCheckpoint:
    final = FinalReport(
        title="",
        summary="Measured evidence documents figures [[p1]].",
        sections=(("Findings", "Collected data frame the observed window [[p1]]."),),
    )
    return WriterCheckpoint(
        final=final,
        draft_sha256=hashlib.sha256(final.markdown.encode()).hexdigest(),
        stage="review",
        budget=ReviewBudget(3),
        **extra,
    )


def test_a_writer_checkpoint_round_trips_its_source_notes() -> None:
    state = _checkpoint(
        source_notes={"p1": SourceNotes(source_sha256="a" * 64, chunks=2, findings_rejected=1)}
    )

    restored = WriterCheckpoint.model_validate(json.loads(state.model_dump_json()))

    assert restored.source_notes["p1"].source_sha256 == "a" * 64
    assert restored.source_notes["p1"].chunks == 2
    assert restored.source_notes["p1"].findings_rejected == 1


def test_a_checkpoint_written_before_source_reading_still_loads() -> None:
    stored = json.loads(_checkpoint().model_dump_json())
    del stored["source_notes"]

    assert WriterCheckpoint.model_validate(stored).source_notes == {}


# ---------------------------------------------------------------------------
# The writer actually reads the notes.
# ---------------------------------------------------------------------------


_REPORT = report_markdown(
    "Measured evidence about the subject documents the reported figures [[s1]].",
    [("Findings", "Collected data from the observed window frame the comparison [[s1]].")],
)


async def test_write_report_reads_a_long_source_and_puts_its_notes_in_the_draft_prompt() -> None:
    # Larger than the whole 220,000-character pool, which is the case notes
    # exist for: the writer cannot be shown this source whole.
    passage = _source(
        "Measured evidence about the subject, with reported figures and collected "
        "data from the observed window. " * 2_400
    )
    # Over 220,000 characters, so the pool cannot render it whole and the notes
    # are what the writer reads; at 100,000 per chunk that is three reads.
    reader_reply = _reply(
        [
            {
                "quote": "reported figures and collected data from the observed window",
                "statement": "The source reports figures for the observed window",
                "conditions": "observed window only",
                "kind": "result",
            }
        ]
    )
    router = RecordingRouter(
        [reader_reply, reader_reply, reader_reply, _REPORT, '{"passes": true, "failures": []}']
    )
    _captured, emit = collect_emits()

    written = await write_report(
        "the state of X",
        outcome([passage]),
        router=router,
        nli=_FakeNLI(),
        bound=bounds_for("quick"),
        emit=emit,
        conversation_id="conv_writer",
    )

    assert router.stages[:4] == ["source_reading"] * 3 + ["report_draft"]
    draft_prompt = router.prompt(3)
    assert NOTES_HEADER in draft_prompt
    assert "The source reports figures for the observed window" in draft_prompt
    assert "Condition: observed window only." in draft_prompt
    assert "stated condition in the same sentence or table cell" in draft_prompt
    assert [row for row in written.trail if row["kind"] == "source_reading"] == [
        {
            "kind": "source_reading",
            "source_id": "p1",
            "source_sha256": hashlib.sha256(passage.text.encode()).hexdigest(),
            "chunks": 3,
            "findings_accepted": 1,
            "findings_rejected": 0,
            "ok": True,
            "error": None,
        }
    ]


def test_notes_never_displace_a_source_the_pool_can_render_whole() -> None:
    passage = _source("Capacity reached 12 GW in the 2024 fleet. " * 220)
    block = "\n".join([NOTES_HEADER, '- [chars 0:10] "Capacity r" — a. Condition: b.'])

    rendered = format_evidence_pool(
        [passage], char_budget=40_000, query="capacity", notes={passage.id: block}
    )

    assert passage.text.strip() in rendered
    assert NOTES_HEADER not in rendered


# ---------------------------------------------------------------------------
# Extraction artefacts inside words.
# ---------------------------------------------------------------------------


def test_a_quote_repairing_a_word_the_extraction_split_is_accepted() -> None:
    # A PDF extraction put a space inside "outflow"; the reader quotes the word
    # as a person reads it. Measured live on TRCA's report, where this rejected
    # 24 of 25 otherwise-good findings on the one source carrying the answer.
    text = "Overall, the PPs reduced the total volume of stormwater ou tflow by 42%."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "the total volume of stormwater outflow by 42%",
                "statement": "Outflow volume fell 42%",
                "conditions": "September 2010 to June 2012",
                "kind": "result",
            }
        ],
        "contrary": [],
    }

    notes = notes_from_payload(payload, text)

    assert notes.findings_rejected == 0
    kept = notes.findings[0]
    assert text[kept.start : kept.end] == "the total volume of stormwater ou tflow by 42%"


def test_a_soft_hyphen_or_non_breaking_space_does_not_fail_a_real_quote() -> None:
    # Extraction leaves a soft hyphen where a word broke across lines, a
    # non-breaking space between a number and its unit, and an en dash where
    # the page had one. None of them changes a word the reader can see.
    text = "Rec\u00adommendations: the stack ran 71\u00a0% at five\u2013hour duration."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "Recommendations: the stack ran 71 % at five-hour duration.",
                "statement": "71% at five-hour duration",
                "conditions": "five-hour duration",
                "kind": "result",
            }
        ],
        "contrary": [],
    }

    notes = notes_from_payload(payload, text)

    assert notes.findings_rejected == 0
    assert (notes.findings[0].start, notes.findings[0].end) == (0, len(text))


def test_a_digit_separated_by_a_hyphen_is_not_the_same_number() -> None:
    # The wall removes separators, never a hyphen: "10-6 cm/s" is 10^-6 in this
    # corpus and must not be matched by a quote that says "106 cm/s".
    text = "Saturated hydraulic conductivity ranges between 10-6 and 10-4 cm/s."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "conductivity ranges between 106 and 104 cm/s",
                "statement": "Conductivity is 106 to 104 cm/s",
                "conditions": "none stated",
                "kind": "result",
            }
        ],
        "contrary": [],
    }

    assert notes_from_payload(payload, text).findings == []


def test_a_quote_the_source_does_not_contain_is_still_rejected() -> None:
    text = "Overall, the PPs reduced the total volume of stormwater ou tflow by 42%."
    payload = {
        "sections": [],
        "findings": [
            {
                "quote": "the total volume of stormwater outflow by 84%",
                "statement": "Outflow volume fell 84%",
                "conditions": "none stated",
                "kind": "result",
            }
        ],
        "contrary": [],
    }

    notes = notes_from_payload(payload, text)

    assert notes.findings == []
    assert notes.findings_rejected == 1


def test_a_source_of_a_quarter_million_characters_is_read_in_three_chunks() -> None:
    marks = (
        "Alpha marker: capacity reached 12 GW in 2024.",
        "Bravo marker: the pilot ran eleven days only.",
        "Charlie marker: efficiency was 71% at five hours.",
    )
    filler = "filler sentence about the subject. "
    text = ""
    for index, mark in enumerate(marks):
        text += mark + " " + filler * ((100_000 - len(mark) - 2) // len(filler))
        text = text[: 100_000 * (index + 1)].ljust(100_000 * (index + 1), "x")
    passage = _source(text[:250_000])
    router = RecordingRouter(
        [
            _reply(
                [
                    {
                        "quote": mark,
                        "statement": f"mark {index}",
                        "conditions": "2024",
                        "kind": "result",
                    }
                ]
            )
            for index, mark in enumerate(marks)
        ]
    )
    _captured, emit = collect_emits()

    notes = await_sync(
        read_sources(
            [passage],
            query="capacity",
            router=router,
            conversation_id=None,
            emit=emit,
            should_cancel=None,
        )
    )

    assert router.calls == 3
    assert notes["p1"].chunks == 3
    assert [f.statement for f in notes["p1"].findings] == ["mark 0", "mark 1", "mark 2"]


async def test_the_per_run_reader_bound_stops_before_the_next_source(monkeypatch) -> None:
    monkeypatch.setattr(_source_reading, "MAX_READER_CALLS", 2)
    passages = [
        _source("Capacity reached 12 GW in 2024. " * 400, f"p{index}") for index in (1, 2, 3)
    ]
    router = RecordingRouter([_reply([]), _reply([])])
    _captured, emit = collect_emits()

    notes = await read_sources(
        passages,
        query="capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert router.calls == 2
    assert list(notes) == ["p1", "p2"]


async def test_a_reader_call_asks_the_provider_not_to_think() -> None:
    # Measured: DeepSeek spent the whole 32,000-token ceiling on hidden
    # reasoning and returned no JSON on 7 of 18 reads. Extraction over text
    # already in the prompt does not need a reasoning phase.
    passage = _source("Capacity reached 12 GW in 2024. " * 400)
    router = RecordingRouter([_reply([])])
    _captured, emit = collect_emits()

    await read_sources(
        [passage],
        query="capacity",
        router=router,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert router.requests[0].enable_thinking is False
    assert router.requests[0].temperature == 0.0


def test_a_share_ending_mid_note_drops_that_note_whole_not_its_condition() -> None:
    # A note trimmed in half would show a number with its Condition clause cut
    # off — exactly the defect the condition-carry wall exists to prevent.
    text = "Alpha result sentence. Bravo result sentence. "
    payload = {
        "sections": [],
        "findings": [
            _finding("result", "Alpha result sentence.", "alpha"),
            _finding("result", "Bravo result sentence.", "bravo"),
        ],
        "contrary": [],
    }
    block = render_source_notes(notes_from_payload(payload, text))
    lines = block.split("\n")
    mid_second_note = len(lines[0]) + 1 + len(lines[1]) + 1 + len(lines[2]) // 2

    passage = _source("Alpha result sentence. Bravo result sentence. " * 200)
    header_chars = len(f"[p1] {passage.source_title}\n{passage.source_url}\n")
    rendered = format_evidence_pool(
        [passage],
        char_budget=header_chars + mid_second_note,
        query="alpha",
        notes={passage.id: block},
    )

    body = rendered.split("\n", 2)[2]
    for line in body.split("\n"):
        if line.startswith("- "):
            assert "Condition:" in line
    assert "bravo" not in body

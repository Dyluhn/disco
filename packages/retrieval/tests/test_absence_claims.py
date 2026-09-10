"""A sentence that says the record lacks something is a claim about the record.

Measured on Q4-GLM: the report said the standards "could not be verified from
the admitted text" while citing a source whose table states them, and the
reviewer had no reason to look. An assertion of absence is checkable against
the very source it cites, so it is checked — deterministically located, and
handed to the reviewer with the spans that contradict it.
"""

from __future__ import annotations

import json

from disco.retrieval.deep_research._absence_claims import (
    ABSENCE_PHRASES,
    absence_claims,
    absence_context,
)
from disco.retrieval.deep_research.quality_audit import ClaimRecord
from disco.retrieval.models import Passage

_TABLE = (
    "Table II.2 Energy Conservation Standards\n"
    "| Electric Open (Coil) Element Cooking Tops | No Standard |\n"
    "| Electric Smooth Element Cooking Tops | 207 _kWh/year_ |\n"
)


def _passage(text: str = _TABLE) -> Passage:
    return Passage(
        id="p1",
        source_url="https://example.test/rule",
        source_title="Federal Register rule",
        text=text,
    )


def _claim(text: str, sources=("p1",)) -> ClaimRecord:
    return ClaimRecord(claim_id="c1", text=text, source_ids=tuple(sources), section_id="s1")


def test_the_phrase_list_is_one_list_and_every_phrase_tags_a_sentence() -> None:
    for phrase in ABSENCE_PHRASES:
        claim = _claim(f"The standard levels {phrase} in the record here.")
        assert absence_claims([claim]) == [claim], phrase


def test_an_absence_assertion_is_tagged_and_the_contradicting_row_is_offered() -> None:
    claim = _claim(
        "The stringency levels for electric smooth element cooking tops "
        "could not be verified from the admitted text."
    )

    tagged = absence_claims([claim])
    context = absence_context(tagged, {"p1": _passage()})

    assert tagged == [claim]
    # The row that settles it is offered, with an offset that really points at it.
    assert "207" in context
    rows = json.loads(context.split("preserved:\n", 1)[1])
    spans = rows[0]["found_in_the_cited_source"]
    assert any("207" in span["text"] for span in spans)
    assert all(_TABLE[span["start"] : span["end"]] for span in spans)


def test_an_absence_assertion_about_something_truly_absent_offers_no_span() -> None:
    claim = _claim(
        "Hydrogen electrolyser stringency levels could not be verified from the admitted text."
    )

    context = absence_context(absence_claims([claim]), {"p1": _passage()})

    assert "no span in the cited source" in context
    assert "searched" in context


def test_a_sentence_that_asserts_nothing_about_the_record_is_not_tagged() -> None:
    claim = _claim("Electric smooth element cooking tops are limited to 207 kWh per year.")

    assert absence_claims([claim]) == []


def test_the_candidate_spans_handed_over_are_bounded() -> None:
    text = "\n".join(f"Row {index} standard level 207 kWh recorded." for index in range(400))
    claim = _claim("The 207 kWh standard level could not be verified from the admitted text.")

    context = absence_context(absence_claims([claim]), {"p1": _passage(text)})

    assert context.count("[chars ") <= 5
    assert len(context) < 6_000

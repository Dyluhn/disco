"""A number taken from a note must arrive with the condition the source stated.

Measured on E2-DeepSeek: "five-hour duration" was in the writer prompt five
times and the report still described Jimusar with no duration; the "projected
2030" label sat in the note beside "$18/kWh" and the draft used the figure
bare. Both are a string check away from being caught, so a program catches
them and the model never gets a vote.
"""

from __future__ import annotations

from disco.retrieval.deep_research._condition_carry import condition_carry_findings
from disco.retrieval.deep_research._source_reading import SourceFinding, SourceNotes


def _notes(quote: str, conditions: str, statement: str = "a figure") -> dict[str, SourceNotes]:
    return {
        "p1": SourceNotes(
            findings=[
                SourceFinding(
                    quote=quote,
                    statement=statement,
                    conditions=conditions,
                    kind="result",
                    start=0,
                    end=len(quote),
                )
            ]
        )
    }


_NOTES = _notes(
    "installed ESS costs fall to $18/kWh",
    "projected 2030 installed ESS costs, 100 MW, 100 h",
)


def test_a_number_used_without_its_stated_condition_is_flagged() -> None:
    sections = [("Costs", "Storage reaches $18/kWh at utility scale [[p1]].")]

    findings = condition_carry_findings("", sections, _NOTES)

    assert len(findings) == 1
    assert "$18/kWh" in findings[0].quote
    assert "projected" in findings[0].fix
    assert "2030" in findings[0].fix
    assert "p1" in findings[0].fix


def test_the_same_number_carrying_its_condition_is_not_flagged() -> None:
    sections = [("Costs", "Projected 2030 installed ESS costs fall to $18/kWh at 100 MW [[p1]].")]

    assert condition_carry_findings("", sections, _NOTES) == []


def test_a_number_that_matches_no_note_is_ignored() -> None:
    sections = [("Costs", "Storage reached $47/kWh in 2024 [[p1]].")]

    assert condition_carry_findings("", sections, _NOTES) == []


def test_a_table_cell_is_checked_like_a_sentence() -> None:
    notes = _notes("Jimusar delivers 350 MW", "five-hour duration, 2023 commissioning")
    sections = [("Plants", "| Plant | Rating |\n| --- | --- |\n| Jimusar | 350 MW [[p1]] |")]

    findings = condition_carry_findings("", sections, notes)

    assert len(findings) == 1
    assert "five-hour" in findings[0].fix


def test_a_cell_that_carries_the_duration_is_not_flagged() -> None:
    notes = _notes("Jimusar delivers 350 MW", "five-hour duration, 2023 commissioning")
    sections = [("Plants", "| Jimusar | 350 MW over five-hour duration, 2023 [[p1]] |")]

    assert condition_carry_findings("", notes and sections, notes) == []


def test_a_note_stating_no_condition_never_flags() -> None:
    notes = _notes("output was 350 MW", "none stated")
    sections = [("Plants", "Output reached 350 MW [[p1]].")]

    assert condition_carry_findings("", sections, notes) == []


def test_only_the_cited_source_notes_are_consulted() -> None:
    sections = [("Costs", "Storage reaches $18/kWh at utility scale [[p2]].")]

    assert condition_carry_findings("", sections, _NOTES) == []

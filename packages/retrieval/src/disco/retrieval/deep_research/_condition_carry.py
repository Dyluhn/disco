"""A number from a note must arrive with the condition its source stated.

Measured on E2-DeepSeek. "five-hour duration" reached the writer prompt five
times and the report still described the Jimusar plant with a rating and no
duration. The note for "$18/kWh" carried "projected 2030 installed ESS costs"
and the draft used the figure as a current price. Both are the same defect: the
number survived the trip from source to report and the condition that makes it
mean something did not.

That defect is a string comparison, so no model judges it. A draft sentence or
table cell holding a number that a cited source's note also holds is checked
for the note's distinctive condition terms; a sentence missing them becomes a
finding on the existing repair path, naming the number, the source, the note's
offset and the condition text to carry. A number no note holds is not this
check's business, and a note whose source stated no condition can never fail.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..source_excerpts import evidence_terms
from ._report_rulers import CITATION_MARKER
from ._source_notes import SourceFinding, SourceNotes
from ._writer_findings import SUMMARY_WHERE, Finding

KIND_CONDITION = "condition_carry"

#: A number as a report writes one: 18, 2030, 3.5, 57.6, 100. Currency and unit
#: symbols are matched around it rather than inside it, so "$18/kWh" and
#: "18 kWh" both reduce to the same token.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

#: What a source says a number holds under, once the ordinary words are gone.
#: These survive `evidence_terms` and are the ones a draft has to repeat.
_CONDITION_WORDS = frozenset(
    {
        "projected",
        "modelled",
        "modeled",
        "estimated",
        "forecast",
        "forecasted",
        "target",
        "nominal",
        "pilot",
        "prototype",
        "planned",
        "design",
        "theoretical",
        "simulated",
        "expected",
    }
)

#: A condition that says nothing cannot be carried, and must never flag.
_NO_CONDITION = frozenset({"none stated", "none", "not stated", "unstated", ""})

#: Enough of the condition to count as carried. A draft that repeats the year
#: and the scenario word has carried it; demanding every token would flag
#: honest prose for dropping a unit it spelled differently.
_REQUIRED_SHARE = 0.5


def _numbers(text: str) -> set[str]:
    return {match.group(0).replace(",", "") for match in _NUMBER.finditer(text)}


def condition_terms(conditions: str) -> set[str]:
    """The distinctive part of a stated condition: numbers, units, scenarios."""
    if " ".join(conditions.split()).casefold() in _NO_CONDITION:
        return set()
    terms = {term for term in evidence_terms(conditions) if len(term) > 2}
    numbers = _numbers(conditions)
    hyphenated = {part.casefold() for part in re.findall(r"[A-Za-z]+-[A-Za-z]+", conditions)}
    return (
        {term for term in terms if term in _CONDITION_WORDS}
        | numbers
        | hyphenated
        | {term for term in terms if any(character.isdigit() for character in term)}
    )


def _carried(unit: str, wanted: set[str]) -> bool:
    """Whether this sentence or cell repeats enough of the condition."""
    if not wanted:
        return True
    folded = " ".join(unit.casefold().split())
    present = sum(1 for term in wanted if term in folded)
    return present >= max(1, int(len(wanted) * _REQUIRED_SHARE + 0.999))


def _units(body: str) -> list[str]:
    """The sentences and table cells a condition can be carried in."""
    units: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith("|"):
            units.extend(cell.strip() for cell in line.strip().strip("|").split("|"))
        else:
            units.extend(re.split(r"(?<=[.!?])\s+", line))
    return [unit for unit in units if unit.strip()]


_CITED_ID = re.compile(r"\[\[([^\]]+)\]\]")


def _cited(unit: str) -> set[str]:
    return {match.group(1).strip() for match in _CITED_ID.finditer(unit)}


def _matching_note(unit_numbers: set[str], notes: Sequence[SourceFinding]) -> SourceFinding | None:
    for finding in notes:
        if condition_terms(finding.conditions) and _numbers(finding.quote) & unit_numbers:
            return finding
    return None


def condition_carry_findings(
    summary: str,
    sections: Sequence[tuple[str, str]],
    notes: Mapping[str, SourceNotes],
) -> list[Finding]:
    """Every sentence or cell using a note's number without the note's condition."""
    if not notes:
        return []
    findings: list[Finding] = []
    parts = [(SUMMARY_WHERE, summary), *sections]
    for where, body in parts:
        for unit in _units(body):
            numbers = _numbers(CITATION_MARKER.sub("", unit))
            if not numbers:
                continue
            for source_id in _cited(unit):
                note = notes.get(source_id)
                if note is None:
                    continue
                match = _matching_note(numbers, note.findings)
                if match is None:
                    continue
                wanted = condition_terms(match.conditions)
                if _carried(unit, wanted):
                    continue
                findings.append(
                    Finding(
                        where=where,
                        quote=unit.strip()[:300],
                        fix=(
                            f"This number is taken from [[{source_id}]] at characters "
                            f"{match.start}:{match.end}, where the source states it holds "
                            f'under "{match.conditions}". Carry that condition in this same '
                            "sentence or cell, or drop the number."
                        ),
                        kind=KIND_CONDITION,
                    )
                )
                break
    return findings


__all__ = ["KIND_CONDITION", "condition_carry_findings", "condition_terms"]

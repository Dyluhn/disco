"""The notes one source read produced, and how the writer is shown them.

Kept apart from the reader that fills them because three other things need the
shape without needing the calls: the writer checkpoint persists it, the
evidence pool renders it, and the condition-carry ruler reads a note's stated
condition back out of it. The reader imports the writer's transport, so a
module holding both would close a cycle around every one of them.

A quote is evidence only if the source contains it. Every quote is located in
the source text before the writer sees it, through the extraction-tolerant
matcher in `..text_match`, and the character offsets rendered beside it are the
ones the text actually has. A quote that is not in the source is dropped and
counted; it is never rendered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..text_match import locate, normalized_index

#: Findings kept from one reply. There is no separate cap on how much of a
#: source's share its notes may occupy: the notes ARE that source's evidence,
#: and the share itself is the only bound they need.
MAX_FINDINGS = 40

NOTES_HEADER = "NOTES (validated quotes; offsets are exact)"
CONTRARY_MARK = "Contrary to the source's own headline: "

FindingKind = Literal["result", "method", "limitation", "projection", "claim", "table"]

#: One order over everything a read produced, deciding what a tight share
#: loses first. A stated result, a table row, a limitation and a projection
#: each carry a number and the condition on it; something the source says
#: against its own headline outranks a bare claim, because that is the line a
#: draft is most likely to smooth over; a method note is the one a reader can
#: most easily do without.
_CONTRARY_PRIORITY = 4
_KIND_PRIORITY: dict[str, int] = {
    "result": 0,
    "table": 1,
    "limitation": 2,
    "projection": 3,
    "claim": 5,
    "method": 6,
}


# ---------------------------------------------------------------------------
# What one source read produced.
# ---------------------------------------------------------------------------


class SourceFinding(BaseModel):
    """One quantitative or structural fact, with the condition the source
    states for it and the exact span the quote occupies in the source text."""

    model_config = ConfigDict(extra="forbid")
    quote: str
    statement: str
    conditions: str
    kind: FindingKind
    start: int
    end: int


class ContraryNote(BaseModel):
    """Something the source says against its own headline, quoted and located."""

    model_config = ConfigDict(extra="forbid")
    quote: str
    statement: str
    start: int
    end: int


class SourceNotes(BaseModel):
    """The validated notes for one source, plus what the read itself did.

    ``ok`` is about the READ, not the source: false means a reply never parsed
    or a provider failed, and the writer falls back to that source's windows.
    """

    model_config = ConfigDict(extra="forbid")
    source_sha256: str = ""
    sections: list[str] = Field(default_factory=list)
    findings: list[SourceFinding] = Field(default_factory=list)
    contrary: list[ContraryNote] = Field(default_factory=list)
    chunks: int = 0
    findings_rejected: int = 0
    ok: bool = True
    error: str | None = None


# ---------------------------------------------------------------------------
# The validation wall: a quote is evidence only if the source contains it.
# ---------------------------------------------------------------------------


def _normalized(text: str) -> tuple[str, list[int]]:
    """The source prepared for quote matching (`..text_match`)."""
    return normalized_index(text)


def locate_quote(quote: str, index: tuple[str, list[int]]) -> tuple[int, int] | None:
    """The `(start, end)` the quote occupies in the source, or None if it is
    not there.

    The returned span covers the source's OWN characters, extraction artefacts
    included, so `text[start:end]` is exactly what the page holds there even
    when the reader's quote spells a split word whole.
    """
    return locate(quote, index)


def _finding_kind(raw: str) -> FindingKind:
    """The kind the reader declared, or ``claim`` when it named another."""
    match raw:
        case "result" | "method" | "limitation" | "projection" | "table":
            return raw
        case _:
            return "claim"


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)][:MAX_FINDINGS]


def _text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def notes_from_payload(payload: Mapping[str, Any], text: str) -> SourceNotes:
    """One reader reply, reduced to the part of it the source text proves.

    Findings and contrary notes are kept only when their quote occurs in the
    source; the rest are counted in ``findings_rejected`` and never rendered.
    """
    index = _normalized(text)
    rejected = 0
    findings: list[SourceFinding] = []
    for item in _rows(payload.get("findings")):
        span = locate_quote(_text(item, "quote"), index)
        if span is None:
            rejected += 1
            continue
        findings.append(
            SourceFinding(
                quote=_text(item, "quote"),
                statement=_text(item, "statement"),
                conditions=_text(item, "conditions") or "none stated",
                kind=_finding_kind(_text(item, "kind")),
                start=span[0],
                end=span[1],
            )
        )
    contrary: list[ContraryNote] = []
    for item in _rows(payload.get("contrary")):
        span = locate_quote(_text(item, "quote"), index)
        if span is None:
            rejected += 1
            continue
        contrary.append(
            ContraryNote(
                quote=_text(item, "quote"),
                statement=_text(item, "statement"),
                start=span[0],
                end=span[1],
            )
        )
    sections = [
        " ".join(str(item).split())
        for item in (payload.get("sections") or [])
        if isinstance(item, str) and item.strip()
    ]
    return SourceNotes(
        sections=sections[:MAX_FINDINGS],
        findings=findings,
        contrary=contrary,
        findings_rejected=rejected,
    )


# ---------------------------------------------------------------------------
# What the writer is shown, and what the run records.
# ---------------------------------------------------------------------------


def _finding_line(finding: SourceFinding) -> str:
    return (
        f'- [chars {finding.start}:{finding.end}] "{finding.quote}" — '
        f"{finding.statement}. Condition: {finding.conditions}."
    )


def _contrary_line(note: ContraryNote) -> str:
    return f'- [chars {note.start}:{note.end}] "{note.quote}" — {CONTRARY_MARK}{note.statement}.'


def _ordered_lines(notes: SourceNotes) -> list[str]:
    """Every note this source produced, weakest last.

    One order over findings and contrary items together, so what a tight share
    loses is the least useful line rather than whichever kind happens to be
    rendered second. Rendering in this order is what makes the share itself the
    cap: the evidence pool trims trailing lines, and trailing means weakest.
    """
    ranked: list[tuple[int, int, str]] = [
        (_KIND_PRIORITY[finding.kind], index, _finding_line(finding))
        for index, finding in enumerate(notes.findings)
    ]
    ranked.extend(
        (_CONTRARY_PRIORITY, index, _contrary_line(note))
        for index, note in enumerate(notes.contrary)
    )
    return [line for _priority, _index, line in sorted(ranked)]


def render_source_notes(notes: SourceNotes, *, char_cap: int | None = None) -> str:
    """One source's NOTES block, weakest note last.

    There is no per-source character cap by default. A 6,000-character one
    discarded 172 of S2's 286 accepted findings — including the 42% outflow
    result the report needed — while keeping every contrary item, because it
    exhausted findings before it touched them. The source's own share of the
    evidence pool is the bound, and the pool applies it by dropping trailing
    lines, which this ordering makes the same thing as dropping the weakest.

    `char_cap` is for callers that want a smaller bound than the share.
    """
    lines = _ordered_lines(notes)
    if not lines:
        return ""
    while lines:
        block = "\n".join([NOTES_HEADER, *lines])
        if char_cap is None or len(block) <= char_cap:
            return block
        lines.pop()
    return ""


def notes_blocks(notes: Mapping[str, SourceNotes]) -> dict[str, str]:
    """The rendered NOTES block for every source that produced one."""
    rendered = {source_id: render_source_notes(note) for source_id, note in notes.items()}
    return {source_id: block for source_id, block in rendered.items() if block}


__all__ = [
    "CONTRARY_MARK",
    "MAX_FINDINGS",
    "NOTES_HEADER",
    "ContraryNote",
    "SourceFinding",
    "SourceNotes",
    "locate_quote",
    "notes_blocks",
    "notes_from_payload",
    "render_source_notes",
]

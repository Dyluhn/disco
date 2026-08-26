"""Pure report → authored-deck handoff seam.

The route/loop owner is responsible for appending the eventual job and tool
events. This module only turns one authoritative ``ReportEvent`` into the
typed inputs that the existing structured slide pipeline consumes. Keeping the
typed event (rather than a newly summarized goal) makes the report's facts
available to both authoring stages and gives the host one small, testable fan-in
seam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from disco.core import ReportEvent

DeckFormat = Literal["pptx"]


_MAX_SOURCE_CHARS = 96_000
_MAX_QUERY_CHARS = 4_000
_MAX_SUMMARY_CHARS = 16_000
_MAX_SECTION_TITLE_CHARS = 1_000
_MAX_SECTION_BODY_CHARS = 18_000
_MAX_PASSAGE_EXCERPT_CHARS = 4_000
_MAX_SECTIONS = 32
_MAX_CITED_PASSAGES = 128
_PASSAGE_TEXT_KEYS = ("excerpt", "quote", "text", "content", "markdown", "snippet")


def _serialize_json(value: dict[str, object]) -> str:
    """Serialize bounded prompt data without exposing delimiter characters."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded.translate(str.maketrans({"<": r"\u003c", ">": r"\u003e", "&": r"\u0026"}))


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    marker = "\n[…truncated…]"
    return text[: max(0, limit - len(marker))] + marker


def _source_projection(report: ReportEvent) -> dict[str, object]:
    """Build the allowlisted, bounded report context used by slide authoring.

    Section prose and cited excerpts are untrusted source data. They are labelled
    in-band so report text cannot masquerade as host instructions. Discovery hits,
    reviewed passages, claims, probes, and verification ledgers are intentionally
    excluded from this handoff.
    """
    cited_ids = {
        passage_id
        for section in report.sections[:_MAX_SECTIONS]
        for passage_id in section.cited_passage_ids
    }
    passages: list[dict[str, str]] = []
    for raw in report.passages:
        passage_id = str(raw.get("id", ""))
        if not passage_id or passage_id not in cited_ids:
            continue
        excerpt = next(
            (raw[key] for key in _PASSAGE_TEXT_KEYS if isinstance(raw.get(key), str)),
            "",
        )
        passages.append(
            {
                "id": _bounded_text(passage_id, 300),
                "title": _bounded_text(raw.get("source_title") or raw.get("title", ""), 600),
                "url": _bounded_text(raw.get("source_url") or raw.get("url", ""), 1_200),
                "excerpt": _bounded_text(
                    f"[UNTRUSTED SOURCE EXCERPT]\n{excerpt}",
                    _MAX_PASSAGE_EXCERPT_CHARS,
                ),
            }
        )
        if len(passages) >= _MAX_CITED_PASSAGES:
            break

    return {
        "query": _bounded_text(report.query, _MAX_QUERY_CHARS),
        "summary": _bounded_text(report.summary, _MAX_SUMMARY_CHARS),
        "sections": [
            {
                "id": _bounded_text(section.id, 300),
                "title": _bounded_text(section.title, _MAX_SECTION_TITLE_CHARS),
                "markdown": _bounded_text(
                    f"[UNTRUSTED REPORT SECTION]\n{section.markdown}",
                    _MAX_SECTION_BODY_CHARS,
                ),
                "cited_passage_ids": [
                    _bounded_text(passage_id, 300)
                    for passage_id in section.cited_passage_ids[:_MAX_CITED_PASSAGES]
                ],
            }
            for section in report.sections[:_MAX_SECTIONS]
        ],
        "cited_passages": passages,
    }


def serialize_report_for_deck(report: ReportEvent) -> str:
    """Return deterministic bounded JSON for the slide prompt boundary."""
    projection = _source_projection(report)
    encoded = _serialize_json(projection)
    if len(encoded.encode("utf-8")) <= _MAX_SOURCE_CHARS:
        return encoded
    # Keep the boundary valid JSON even when a report contains the maximum number
    # of maximum-sized sections/passages. The typed event remains authoritative;
    # this compact form is only the prompt projection and retains the most useful
    # top-level source fields without leaking any non-allowlisted report state.
    compact = {
        "query": _bounded_text(report.query, _MAX_QUERY_CHARS),
        "summary": _bounded_text(report.summary, _MAX_SUMMARY_CHARS),
        "sections": [],
        "cited_passages": [],
    }
    encoded = _serialize_json(compact)
    if len(encoded.encode("utf-8")) <= _MAX_SOURCE_CHARS:
        return encoded
    # Defensive fallback for pathological Unicode expansion. This branch still
    # returns a valid allowlisted object and never emits an over-cap string.
    compact["summary"] = ""
    compact["query"] = _bounded_text(report.query, 4_000)
    encoded = _serialize_json(compact)
    return encoded


@dataclass(frozen=True)
class ReportDeckJob:
    """Typed, side-effect-free inputs for the report-to-deck executor seam."""

    report: ReportEvent
    goal: str
    filename: str = "deck"
    format: DeckFormat = "pptx"

    @classmethod
    def from_report(cls, report: ReportEvent, *, filename: str = "deck") -> ReportDeckJob:
        return cls(
            report=report,
            goal=report.query,
            filename=filename,
        )

def build_report_deck_job(report: ReportEvent, *, filename: str = "deck") -> ReportDeckJob:
    """Create the canonical structured-deck job for a finished report."""
    return ReportDeckJob.from_report(report, filename=filename)


# Descriptive aliases keep the seam discoverable to host callers without
# creating a second contract or schema.
ReportDeckHandoff = ReportDeckJob
build_report_deck_handoff = build_report_deck_job


__all__ = [
    "ReportDeckHandoff",
    "ReportDeckJob",
    "build_report_deck_handoff",
    "build_report_deck_job",
    "serialize_report_for_deck",
]

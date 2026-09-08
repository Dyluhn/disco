"""Report normalization and the run invariants.

The normalizer faithfully carries what the wire had and never manufactures
missing structure; ``check_invariants`` judges the artifact as-is and fails
honestly when the product violated its contract.  Each helper below owns one
invariant decision so no single callable re-grows past the complexity cap.

Three of those invariants — repetition, the substantive-body floor, and R7
process language — deliberately import the PRODUCT's own rulers
(``disco.retrieval.deep_research._report_rulers``) rather than restating the
normalization and thresholds here.  When the two disagree about what "the same
sentence" or "thin" means, a report passes the writer's deterministic review
and fails this batch on the same bytes, and the finding stops being evidence
about the product.  ``development/harness/tests/test_ruler_drift.py`` pins the
agreement on fixture reports; slice 6 established the pattern for the thrash
detector's query identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from disco.retrieval.deep_research._report_rulers import (
    DIAGNOSTIC_LANGUAGE_SUMMARY,
    PARAGRAPH_MIN_WORDS,
    PROCESS_LANGUAGE_BODY,
    PUNCTUATION_ONLY,
    SECTION_MIN_WORDS,
    has_process_language,
    repeated_paragraphs,
    repetition_is_acceptable,
    section_body_is_thin,
)
from disco.retrieval.deep_research._report_rulers import (
    word_count as _shared_word_count,
)

from ._observe import _CITATION, Observation, ResearchRequest, _citation_ids, redact

# `research_harness.py` has always offered these four at their private names and
# the harness tests import them from there, so the surface is kept while the
# VALUES become the product's own rulers.
_SUMMARY_DIAGNOSTIC_LANGUAGE = DIAGNOSTIC_LANGUAGE_SUMMARY
_BODY_PROCESS_LANGUAGE = PROCESS_LANGUAGE_BODY
_PUNCTUATION_ONLY = PUNCTUATION_ONLY
_SECTION_MIN_WORDS = SECTION_MIN_WORDS
# The paragraph clause of `repetition_acceptable` below.  It was the last part
# of a harness verdict the product could not name back: `repetition_acceptable`
# already came from the shared ruler, but nothing in the writer's review
# reported the clause on its own, so a report could fail here on a duplicated
# paragraph while the writer declared it clean.  Same values, one owner.
_PARAGRAPH_MIN_WORDS = PARAGRAPH_MIN_WORDS
_repeated_paragraphs = repeated_paragraphs

_FAILURE_WORDS = (
    "research failed",
    "provider failure",
    "could not produce",
    "could not ground",
    "no sources were",
    "empty report",
    "aborted",
    "timed out",
    "cassette miss",
)
# Report LENGTH is deliberately absent from this file.  The harness used to
# accept a report only inside a per-tier word band, which made "exhaustive" mean
# "long" and rewarded padding; the product now spends a deeper tier on more
# RESEARCH turns and lets the report be as long as the evidence makes it.  Word
# counts are still recorded as telemetry (``_report_word_count``) — they are a
# measurement, not an acceptance criterion.

_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def normalize_report(
    frames: Sequence[Mapping[str, Any]], request: ResearchRequest
) -> dict[str, Any]:
    """Find a ReportEvent/final answer in either live wire shape.

    The result faithfully carries what the wire had.  It never manufactures
    missing structure — an empty summary stays empty and missing sections stay
    ``[]`` — so ``check_invariants`` judges the artifact as-is and fails
    honestly when the product violated its contract.

    That faithfulness now includes ``meta``, which this whitelist used to drop.
    The writer names every sentence its NLI verifier could not support after the
    rework in ``meta.unverified_sentences`` and ships them as written; with the
    field discarded here, an acceptance batch could only report that a run failed
    a ruler and never that the product had said so itself.
    """
    raw = dict(_final_candidate(frames) or {})
    kind = str(raw.get("kind") or "report")
    if kind == "research_checkpoint":
        return redact(
            {
                "kind": kind,
                "query": raw.get("query") or request.query,
                "summary": "",
                "sections": [],
                "passages": _mapping_rows(raw.get("passages")),
                "all_hits": _mapping_rows(raw.get("all_hits")),
                "trail": _plain_list(raw.get("trail")),
                "completed_queries": _plain_list(raw.get("completed_queries")),
                "depth_tier": raw.get("depth_tier") or request.depth,
            }
        )
    summary = str(raw.get("summary") or raw.get("executive_summary") or "").strip()
    result = {
        "kind": "report",
        "query": raw.get("query") or request.query,
        "summary": summary,
        "sections": _mapping_rows(raw.get("sections")),
        "passages": _mapping_rows(raw.get("passages") or raw.get("sources") or []),
        "all_hits": _mapping_rows(raw.get("all_hits") or raw.get("hits") or []),
        "bounded_by": raw.get("bounded_by"),
        "depth_tier": raw.get("depth_tier") or request.depth,
        "claims": _plain_list(raw.get("claims")),
        "completed_probes": _plain_list(raw.get("completed_probes")),
        "pending_probes": _plain_list(raw.get("pending_probes")),
    }
    if raw.get("legacy_replay"):
        result["legacy_replay"] = True
    result["cited_passage_ids"] = sorted(set(_citation_ids(result)))
    # `meta` is added AFTER the citation scan on purpose: the unverified
    # sentences inside it quote the report back, [[id]] markers and all.
    result["meta"] = _report_meta(raw)
    return redact(result)


def _report_meta(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Carry the report's host metadata through, whatever the product put in it.

    This is the one field the normalizer used to drop, and dropping it is how a
    batch reads as "shipped with zero declared residuals" when the product had
    in fact declared every miss on the event log.  A second whitelist here would
    reintroduce exactly that failure the next time the writer learns to declare
    something new, so nothing is selected: whatever `ReportEvent.meta` carried
    reaches the artifact.
    """
    meta = raw.get("meta")
    return dict(meta) if isinstance(meta, Mapping) else {}


def declared_unverified_sentences(report: Mapping[str, Any]) -> list[str]:
    """The sentences the report shipped knowing its verifier could not support.

    The writer runs NLI grounding and one section-scoped rework, then always
    ships; whatever the verifier still could not ground goes out AS WRITTEN and
    is named here, quoted verbatim in report order.  Verbatim matters: an
    operator comparing a failed invariant against this list is asking whether
    the product KNEW, and a paraphrase cannot answer that.
    """
    meta = report.get("meta")
    if not isinstance(meta, Mapping):
        return []
    declared = meta.get("unverified_sentences")
    return [str(line) for line in declared] if isinstance(declared, list) else []


def _final_candidate(frames: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the newest frame that carries a report/final answer, if any."""
    for frame in reversed(frames):
        if isinstance(frame.get("report"), Mapping):
            return cast(Mapping[str, Any], frame["report"])
        event = frame.get("event")
        if isinstance(event, Mapping) and str(event.get("kind", "")).lower() in {
            "report",
            "research_checkpoint",
        }:
            return event
        if str(frame.get("kind", "")).lower() in {"report", "research_checkpoint"}:
            return frame
        answer = frame.get("answer")
        if isinstance(answer, Mapping) and (frame.get("type") == "final" or "sections" in answer):
            return answer
    return None


def _plain_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping_rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in _plain_list(value) if isinstance(item, Mapping)]


def _is_stopped_checkpoint(report: Mapping[str, Any]) -> bool:
    """A typed resumable checkpoint, which is not report content."""
    raw_sections = report.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    return (
        str(report.get("kind") or "") == "research_checkpoint"
        and not sections
        and not str(report.get("summary") or "").strip()
    )


def check_invariants(
    report: Mapping[str, Any],
    markdown: str,
    observer: Observation,
    *,
    depth: str = "standard_deep",
) -> dict[str, bool]:
    # A research_checkpoint is a legitimate terminal artifact and is judged as
    # checkpoint state, not as a report: its empty summary and sections are
    # allowed because the typed event kind carries that contract.
    checkpoint = _is_stopped_checkpoint(report)
    sections = _plain_list(report.get("sections"))
    valid_sections = _valid_sections(sections)
    summary = str(report.get("summary") or "").strip()
    passages = _passages_by_id(report)
    cited = _citation_ids(report)
    body = _section_body_text(valid_sections)
    return {
        "final_report_exists": bool(markdown.strip() and sections) or checkpoint,
        "executive_summary_substantive": _summary_substantive(summary) or checkpoint,
        "executive_summary_synthesizes_report": not _summary_is_section_copy(
            summary, valid_sections
        ),
        "no_empty_or_failure_sections": _sections_complete(sections, valid_sections) or checkpoint,
        "substantive_section_bodies": _sections_substantive(valid_sections, depth) or checkpoint,
        "section_citations_present": _section_citations_ok(valid_sections, set(passages))
        or checkpoint,
        "executive_summary_cited": _summary_citations_ok(summary, set(passages)) or checkpoint,
        "citations_resolve": _citations_resolve(cited, passages) or checkpoint,
        "no_diagnostic_or_research_process_language": not _diagnostic_language(summary, body),
        "heading_hierarchy_valid": _heading_hierarchy_is_valid(markdown) or checkpoint,
        "repetition_acceptable": _repetition_is_acceptable(body) or checkpoint,
        "stopped_checkpoint_valid": checkpoint
        or str(report.get("kind") or "") != "research_checkpoint",
        "bounds_and_errors_recorded": isinstance(observer.bounds, list)
        and isinstance(observer.errors, list),
    }


def _valid_sections(sections: list[Any]) -> list[Mapping[str, Any]]:
    return [section for section in sections if isinstance(section, Mapping)]


def _passages_by_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Index the report's passages by id (unkeyed passages cannot resolve)."""
    return {
        str(item.get("id")): item
        for item in _plain_list(report.get("passages"))
        if isinstance(item, Mapping) and item.get("id")
    }


def _section_body_text(valid_sections: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(str(section.get("markdown") or "") for section in valid_sections)


def _summary_substantive(summary: str) -> bool:
    return len(summary) >= 24 and not _failure_text(summary)


def _citations_resolve(cited: set[str], passages: Mapping[str, Mapping[str, Any]]) -> bool:
    citation_urls = all(
        str(passages[identifier].get("source_url") or "").startswith(("http://", "https://"))
        for identifier in cited
        if identifier in passages
    )
    return bool(cited) and cited.issubset(passages.keys()) and citation_urls


def _sections_complete(sections: list[Any], valid_sections: Sequence[Mapping[str, Any]]) -> bool:
    return (
        bool(sections)
        and len(valid_sections) == len(sections)
        and all(
            str(section.get("title") or "").strip()
            and str(section.get("markdown") or "").strip()
            and not _failure_text(str(section.get("markdown") or ""))
            for section in valid_sections
        )
    )


def _sections_substantive(valid_sections: Sequence[Mapping[str, Any]], depth: str) -> bool:
    return bool(valid_sections) and all(
        _section_is_substantive(section, depth=depth) for section in valid_sections
    )


def _section_citations_ok(
    valid_sections: Sequence[Mapping[str, Any]], passage_ids: set[str]
) -> bool:
    return bool(valid_sections) and all(
        bool(_citation_ids(section)) and _citation_ids(section).issubset(passage_ids)
        for section in valid_sections
    )


def _summary_citations_ok(summary: str, passage_ids: set[str]) -> bool:
    """The executive summary cites, the same way every section must.

    The writer's own rule is that every factual sentence ends in a citation,
    and every summary the acceptance batches ever shipped carried 14-35 of
    them — until one shipped a ten-word uncited fragment that a rework re-ask
    returned, and `executive_summary_substantive` (24 characters, no failure
    text) passed it. An uncited summary is not the report's answer.
    """
    cited = set(_CITATION.findall(summary))
    return bool(cited) and cited.issubset(passage_ids)


def _diagnostic_language(summary: str, body: str) -> bool:
    return has_process_language(summary, body)


def _summary_sentences(summary: str) -> list[str]:
    sentences = [
        re.sub(r"\[\[[\w-]+\]\]", "", sentence).strip(" \n*-#>.").casefold()
        for sentence in re.split(r"(?<=[.!?])\s+", summary)
    ]
    return [sentence for sentence in sentences if len(sentence.split()) >= 5]


def _summary_is_section_copy(summary: str, valid_sections: Sequence[Mapping[str, Any]]) -> bool:
    # Executive summaries legitimately restate a report's strongest findings.
    # This synthesis gate rejects only a summary copied wholesale from one
    # section; accidental repetition within the body is measured separately by
    # ``_repetition_is_acceptable``.
    summary_sentences = _summary_sentences(summary)
    if len(valid_sections) > 1 and len(summary_sentences) >= 2:
        for section in valid_sections:
            section_body = re.sub(
                r"\[\[[\w-]+\]\]", "", str(section.get("markdown") or "")
            ).casefold()
            copied = sum(sentence in section_body for sentence in summary_sentences)
            if copied / len(summary_sentences) >= 0.75:
                return True
    return False


def _report_word_count(report: Mapping[str, Any]) -> int:
    text = " ".join(
        [
            str(report.get("summary") or ""),
            *[
                str(section.get("markdown") or "")
                for section in report.get("sections", [])
                if isinstance(section, Mapping)
            ],
        ]
    )
    text = _CITATION.sub("", text)
    return len(re.findall(r"\b[\w'-]+\b", text))


def _report_prose(report: Mapping[str, Any]) -> str:
    """Return report prose only; source titles/URLs are not quality prose."""
    return "\n\n".join(
        [
            str(report.get("summary") or ""),
            *[
                str(section.get("markdown") or "")
                for section in report.get("sections", [])
                if isinstance(section, Mapping)
            ],
        ]
    )


def _word_count(text: str) -> int:
    return _shared_word_count(text)


def _section_is_substantive(section: Mapping[str, Any], *, depth: str) -> bool:
    del depth  # The substantive-body floor is absolute, not tier-scaled.
    body = str(section.get("markdown") or "").strip()
    # The floor, the punctuation-only body, and the placeholder body are the
    # product's own ruler.  The failure-text clause below stays here: "cassette
    # miss" and friends are harness vocabulary, not report quality.
    if section_body_is_thin(body):
        return False
    return not _failure_text(body)


def _heading_hierarchy_is_valid(markdown: str) -> bool:
    rows = []
    for line in markdown.splitlines():
        match = _MARKDOWN_HEADING.match(line)
        if match:
            title = match.group(2).strip()
            if not title:
                return False
            rows.append((len(match.group(1)), title))
    if not rows or rows[0][0] != 1:
        return False
    # A report has one document title and then a normal Markdown hierarchy;
    # skipping from H2 to H4 is a structural defect even if the text is long.
    return all(
        level <= previous + 1 for (previous, _), (level, _) in zip(rows, rows[1:], strict=False)
    )


def _repetition_is_acceptable(prose: str) -> bool:
    return repetition_is_acceptable(prose)


def _failure_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _FAILURE_WORDS)

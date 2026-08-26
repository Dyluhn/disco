"""Report normalization and the run invariants.

The normalizer faithfully carries what the wire had and never manufactures
missing structure; ``check_invariants`` judges the artifact as-is and fails
honestly when the product violated its contract.  Each helper below owns one
invariant decision so no single callable re-grows past the complexity cap.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ._observe import _CITATION, Observation, ResearchRequest, _citation_ids, redact

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
_DEPTH_WORD_TARGETS = {
    "quick": (1500, 2500),
    # The upper bounds leave room for a genuinely long report.  They are
    # deliberately generous: the lower bound is the acceptance requirement;
    # prose must still earn its length through the section and repetition
    # checks below.
    "standard_deep": (4000, 7000),
    "exhaustive": (8000, 12000),
}
# The product keeps these nominal tier targets. The harness is an outside
# diagnostic, so it allows a small boundary tolerance for provider variance
# after the writer has produced an otherwise valid report.
_DEPTH_WORD_TOLERANCE_PERCENT = 5


def _depth_word_bounds(depth: str) -> tuple[int, int] | None:
    """Return the harness acceptance range around a nominal tier target."""
    target = _DEPTH_WORD_TARGETS.get(depth)
    if target is None:
        return None
    minimum, maximum = target
    return (
        minimum - minimum * _DEPTH_WORD_TOLERANCE_PERCENT // 100,
        maximum + maximum * _DEPTH_WORD_TOLERANCE_PERCENT // 100,
    )


_SECTION_MIN_WORDS = 120
_SUMMARY_DIAGNOSTIC_LANGUAGE = re.compile(
    r"\b(?:"
    r"research process|retrieval (?:failed|failure|gap|status)|source availability|"
    r"verification (?:failed|failure|status)|"
    r"confidence labels?|confidence scores?|evidence map|subquestions?|"
    r"search queries?|search process|provider response|report structure|"
    r"we searched|we reviewed|we found|could not verify|"
    r"coverage (?:gap|gaps|was)|word count|section count"
    r")\b",
    re.IGNORECASE,
)
_BODY_PROCESS_LANGUAGE = re.compile(
    r"\b(?:research process|retrieval (?:failed|failure|gap|status)|"
    r"verification (?:failed|failure|status)|source availability prevented|"
    r"we searched|we reviewed|search queries?|search process|provider response|"
    r"subquestion (?:failed|status)|word count target|section count target)\b",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_PUNCTUATION_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)


def normalize_report(
    frames: Sequence[Mapping[str, Any]], request: ResearchRequest
) -> dict[str, Any]:
    """Find a ReportEvent/final answer in either live wire shape.

    The result faithfully carries what the wire had.  It never manufactures
    missing structure — an empty summary stays empty and missing sections stay
    ``[]`` — so ``check_invariants`` judges the artifact as-is and fails
    honestly when the product violated its contract.
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
    return redact(result)


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


def _sections_complete(
    sections: list[Any], valid_sections: Sequence[Mapping[str, Any]]
) -> bool:
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
        bool(_citation_ids(section))
        and _citation_ids(section).issubset(passage_ids)
        for section in valid_sections
    )


def _diagnostic_language(summary: str, body: str) -> bool:
    return bool(
        _SUMMARY_DIAGNOSTIC_LANGUAGE.search(summary) or _BODY_PROCESS_LANGUAGE.search(body)
    )


def _summary_sentences(summary: str) -> list[str]:
    sentences = [
        re.sub(r"\[\[[\w-]+\]\]", "", sentence).strip(" \n*-#>.").casefold()
        for sentence in re.split(r"(?<=[.!?])\s+", summary)
    ]
    return [sentence for sentence in sentences if len(sentence.split()) >= 5]


def _summary_is_section_copy(
    summary: str, valid_sections: Sequence[Mapping[str, Any]]
) -> bool:
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
    return len(re.findall(r"\b[\w'-]+\b", _CITATION.sub("", text)))


def _section_is_substantive(section: Mapping[str, Any], *, depth: str) -> bool:
    body = str(section.get("markdown") or "").strip()
    words = _word_count(body)
    if words < _SECTION_MIN_WORDS or _PUNCTUATION_ONLY.fullmatch(body):
        return False
    # A placeholder can have enough repeated punctuation to evade a simple
    # word-count check (for example ``...`` or ``[content omitted]``).
    if re.fullmatch(r"[\s\W]*(?:tbd|n/?a|omitted|placeholder)[\s\W]*", body, re.I):
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
        level <= previous + 1
        for (previous, _), (level, _) in zip(rows, rows[1:], strict=False)
    )


def _repetition_is_acceptable(prose: str) -> bool:
    sentences = [
        re.sub(r"\s+", " ", _CITATION.sub("", sentence)).strip().lower()
        for sentence in re.split(r"(?<=[.!?])\s+", prose)
    ]
    sentences = [sentence for sentence in sentences if _word_count(sentence) >= 5]
    if len(sentences) < 8:
        return False
    counts: dict[str, int] = {}
    for sentence in sentences:
        counts[sentence] = counts.get(sentence, 0) + 1
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    max_repeat = max(counts.values(), default=0)
    if max_repeat >= 4 and repeated / len(sentences) > 0.05:
        return False
    paragraphs = [
        re.sub(r"\s+", " ", _CITATION.sub("", paragraph)).strip().lower()
        for paragraph in re.split(r"\n\s*\n", prose)
    ]
    paragraphs = [paragraph for paragraph in paragraphs if _word_count(paragraph) >= 20]
    paragraph_counts: dict[str, int] = {}
    for paragraph in paragraphs:
        paragraph_counts[paragraph] = paragraph_counts.get(paragraph, 0) + 1
    return max(paragraph_counts.values(), default=0) < 2


def _failure_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _FAILURE_WORDS)

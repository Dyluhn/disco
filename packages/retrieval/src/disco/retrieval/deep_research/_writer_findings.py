"""Findings — the one shape every review result takes, and the deterministic
walls that produce them.

A finding names ONE part of the report (the executive summary or one '## '
section), quotes the text it is about, and says what changes. That shape is
what makes a section-scoped rework possible: the writer is handed only the
parts with findings and returns each of them whole, and the parts without
findings are never re-generated. Every producer here — and the reviewer's JSON
verdict, once `review_findings` has resolved its sections — feeds the same
list, so the rework reads one list in one shape.

The deterministic walls read `_report_rulers`, the same rulers the acceptance
harness grades with, on the same text: the report as `finalize_report` renders
it. Each one blocks a defect a reader would actually notice — a sentence
printed twice, prose narrating the research, a citation that names no source,
an absence asserted over a query the host could never run — and none of them
measures length, section count, or which sources were used. Those are the
model's to judge.

Nothing here edits prose. A finding is a wall with an angle; the writer makes
the edit.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._report_rulers import (
    CITATION_MARKER,
    normalized_paragraphs,
    normalized_sentences,
    process_language_hits,
    repetition_verdict,
)
from ._search_outcomes import query_tokens
from ._untested_angles import untested_angle_tokens

# The `where` of a finding about the executive summary. Section findings use
# the section's exact title.
SUMMARY_WHERE = "executive summary"

KIND_REPEATED = "repeated"
KIND_PROCESS = "process"
KIND_UNRESOLVED_CITATION = "unresolved_citation"
KIND_ABSENCE = "absence"
KIND_UNSUPPORTED = "unsupported"
KIND_REVIEW = "review"
KIND_COVERAGE = "missing_coverage"


@dataclass(frozen=True)
class Finding:
    """One thing the writer changes, in one named part of the report."""

    where: str  # SUMMARY_WHERE or the exact section title
    quote: str  # bounded verbatim quote from that part; may be empty
    fix: str  # the change, stated to the writer
    kind: str  # one of the KIND_* values
    rubric: str = ""  # the rubric item it falls under, when one applies
    after: str | None = None  # insertion anchor for a missing-coverage section


# Bounds so one badly broken draft still produces a readable findings list.
_MAX_REPEATED = 8
_MAX_PROCESS = 8
_MAX_ABSENCE = 6
_MAX_UNSUPPORTED = 24
_MAX_REVIEW = 12
_QUOTE_WORDS = 15

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_CITED_ID = re.compile(r"\[\[([\w-]+)\]\]")

# Host/process vocabulary that must never surface in report prose and that the
# rulers' own R7 patterns do not cover. Evidence scope is reader-facing context,
# so mentioning an evidence pool is not itself a process finding.
HOST_VOCABULARY = re.compile(
    r"\b(?:retrieval gap|provider failure|search process|research process|"
    r"source budget|source slots|wall[- ]clock)\b",
    re.IGNORECASE,
)

# An executive summary that is a failure digest rather than an answer. Narrow
# on purpose: ordinary subject matter (a report about provider failures) does
# not match, a report saying it could not answer does.
DIAGNOSTIC_SUMMARY = re.compile(
    r"(?:\b(?:i|we|this report|the research) (?:was |were |is |are )?"
    r"(?:unable to|could not|couldn't|cannot|can't) "
    r"(?:answer|determine|complete|produce|verify)\b|"
    r"\b(?:insufficient|no sufficient) (?:evidence|information|sources) (?:was|were) "
    r"(?:found|available)\b|"
    r"\b(?:research|verification|provider) (?:failed|failure|unavailable)\b)",
    re.IGNORECASE,
)


def _quote(text: str) -> str:
    """Quote a sentence, bounded, so the writer can find it without the
    findings list turning into a second copy of the report."""
    words = text.split()
    if len(words) <= _QUOTE_WORDS:
        return " ".join(words)
    return " ".join(words[:_QUOTE_WORDS]) + "…"


def _plain_sentences(text: str) -> list[str]:
    """Sentences as a reader sees them: citation markers removed, whitespace
    collapsed, case and wording otherwise untouched so the quote is verbatim."""
    return [
        collapsed
        for raw in _SENTENCE_BREAK.split(text)
        if (collapsed := re.sub(r"\s+", " ", CITATION_MARKER.sub("", raw)).strip())
    ]


def section_body_text(sections: Sequence[tuple[str, str]]) -> str:
    """The report BODY the repetition ruler grades — sections only.

    The executive summary legitimately restates the report's strongest
    findings, so including it would convict every well-formed report of
    repeating itself. The harness measures the same span.
    """
    return "\n\n".join(markdown for _title, markdown in sections)


# ---------------------------------------------------------------------------
# Repetition.
# ---------------------------------------------------------------------------


def _homes(key: str, per_section: Sequence[tuple[str, Counter[str]]]) -> list[tuple[str, int]]:
    """Which sections carry this normalized text, and how many times each."""
    return [(title, counts[key]) for title, counts in per_section if counts.get(key)]


def _repeat_findings(
    repeats: Sequence[Any],
    per_section: Sequence[tuple[str, Counter[str]]],
    *,
    unit: str,
    rubric: str,
) -> list[Finding]:
    """One finding per section that carries a repeat, naming where the text
    already stands so the writer knows which occurrence is the keeper."""
    out: list[Finding] = []
    for repeat in repeats:
        homes = _homes(repeat.text, per_section)
        if not homes:
            continue
        first, first_count = homes[0]
        quote = _quote(repeat.quote)
        if first_count > 1:
            out.append(
                Finding(
                    where=first,
                    quote=quote,
                    fix=(
                        f"This {unit} appears {first_count} times in this section. "
                        "Keep exactly one occurrence; where the others stood, "
                        "either say something the evidence supports and the report "
                        "has not said, or say nothing."
                    ),
                    kind=KIND_REPEATED,
                    rubric=rubric,
                )
            )
        for title, _count in homes[1:]:
            out.append(
                Finding(
                    where=title,
                    quote=quote,
                    fix=(
                        f"This {unit} already appears in '{first}'; the reader has "
                        "read it. Here, either say something the evidence supports "
                        "and the report has not said, or say nothing."
                    ),
                    kind=KIND_REPEATED,
                    rubric=rubric,
                )
            )
    return out


def repetition_findings(sections: Sequence[tuple[str, str]]) -> list[Finding]:
    """Sentences and paragraphs the report body prints more than once.

    The sentence clause and the paragraph clause of the acceptance ruler, each
    located to the sections that carry the text. A repeat that spans two
    sections is one finding per section, and the finding in the later section
    names the earlier one as the keeper — otherwise a writer rewriting both
    parts could keep both, or neither.
    """
    verdict = repetition_verdict(section_body_text(sections))
    findings: list[Finding] = []
    if verdict.sentences_repeat:
        per_section = [(title, Counter(normalized_sentences(md))) for title, md in sections]
        findings.extend(
            _repeat_findings(
                [r for r in verdict.repeated_sentences if r.count > 1][:_MAX_REPEATED],
                per_section,
                unit="sentence",
                rubric="R4",
            )
        )
    if verdict.duplicate_paragraph:
        per_section = [(title, Counter(normalized_paragraphs(md))) for title, md in sections]
        findings.extend(
            _repeat_findings(
                verdict.repeated_paragraphs[:_MAX_REPEATED],
                per_section,
                unit="paragraph",
                rubric="R4",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Process / diagnostic language.
# ---------------------------------------------------------------------------


def process_language_findings(summary: str, sections: Sequence[tuple[str, str]]) -> list[Finding]:
    """Sentences that narrate the research instead of the subject (rubric R7).

    Three patterns, one finding class: the rulers' own R7 patterns (which the
    harness fails a report on), the host vocabulary the rulers do not cover,
    and the failure-digest shape an executive summary can take. Each sentence
    is named once, with the phrase that convicted it.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []

    def add(where: str, sentence: str, phrase: str) -> None:
        key = (where, sentence.casefold())
        if key in seen:
            return
        seen.add(key)
        out.append(
            Finding(
                where=where,
                quote=_quote(sentence),
                fix=(
                    f"This sentence narrates the research ({phrase!r}) rather than "
                    "the subject. Replace it with analysis of the subject, or remove "
                    "it without losing material limitations or uncertainty. Preserve the "
                    "distinction between what these sources do not establish and "
                    "what does not exist. Describe the evidence limit in reader-facing "
                    "terms instead of narrating tools or internal checks."
                ),
                kind=KIND_PROCESS,
                rubric="R7",
            )
        )

    for hit in process_language_hits(summary, sections):
        where = SUMMARY_WHERE if hit.where == "executive summary" else hit.where
        add(where, hit.sentence, hit.phrase)
    spans: list[tuple[str, str]] = [(SUMMARY_WHERE, summary), *sections]
    for where, text in spans:
        for sentence in _plain_sentences(text):
            match = HOST_VOCABULARY.search(sentence)
            if match is None and where == SUMMARY_WHERE:
                match = DIAGNOSTIC_SUMMARY.search(sentence)
            if match is not None:
                add(where, sentence, match.group(0))
    for title, _body in sections:
        match = HOST_VOCABULARY.search(title)
        if match is not None:
            out.append(
                Finding(
                    where=title,
                    quote=title,
                    fix=(
                        f"The heading itself names the research process ({match.group(0)!r}). "
                        "Keep the heading line where it is but make it about the subject."
                    ),
                    kind=KIND_PROCESS,
                    rubric="R7",
                )
            )
    return out[:_MAX_PROCESS]


# ---------------------------------------------------------------------------
# Citations that resolve to nothing.
# ---------------------------------------------------------------------------


def unresolved_citation_findings(
    summary: str,
    sections: Sequence[tuple[str, str]],
    pool_ids: set[str],
    valid_ids_line: str = "",
) -> list[Finding]:
    """Citation markers naming no source in the pool, per part.

    Every citation here is already CANONICAL: the writer's alias citations
    were translated at the boundary, so an id that still fails to resolve is
    one the run never had. One finding per part lists that part's broken ids
    and quotes the first sentence carrying one; `valid_ids_line` is the
    angle — the exact range the writer may cite.
    """
    out: list[Finding] = []
    spans: list[tuple[str, str]] = [(SUMMARY_WHERE, summary), *sections]
    for where, text in spans:
        unknown = sorted({item for item in _CITED_ID.findall(text) if item not in pool_ids})
        if not unknown:
            continue
        quote = ""
        for raw in _SENTENCE_BREAK.split(text):
            if any(f"[[{item}]]" in raw for item in unknown):
                quote = _quote(re.sub(r"\s+", " ", raw).strip())
                break
        listed = ", ".join(f"[[{item}]]" for item in unknown)
        out.append(
            Finding(
                where=where,
                quote=quote,
                fix=(
                    f"These citation markers name no source in the evidence: {listed}. "
                    "Replace each with an [[id]] from the evidence that supports the "
                    "sentence it is on; where no supplied source supports the sentence, "
                    f"rewrite or remove the sentence.{valid_ids_line}"
                ),
                kind=KIND_UNRESOLVED_CITATION,
                rubric="R3",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Absence asserted over an angle research never reached.
# ---------------------------------------------------------------------------
#
# A FROZEN, deliberately small set of strong absence assertions. These are the
# shapes the live defect actually produced ("no source documents a … approval",
# "no randomized-trial evidence", "does not exist") — not a general hedge
# detector. A report is allowed to say the evidence is thin; what it may not do
# is convert a query the host could never run into a fact about the world.
#
# The pattern set alone convicts nothing. It only selects sentences to compare
# against the untested angles; the collision below is the finding.
_ABSENCE_ASSERTIONS = (
    re.compile(r"\bno\b[^.;]{0,80}\bapprovals?\b", re.IGNORECASE),
    re.compile(r"\bno\b[^.;]{0,40}\bevidence\b", re.IGNORECASE),
    re.compile(r"\b(?:does|do)\s+not\s+exist\b", re.IGNORECASE),
    re.compile(r"\b(?:has|have)\s+not\s+been\s+approved\b", re.IGNORECASE),
    re.compile(r"\bnever\s+materiali[sz]ed\b", re.IGNORECASE),
)

# Token-set Jaccard at or above this is "this sentence is about that query".
# Deliberately high: the block in the writer's prompt is the intervention, and
# this check is the assist that names a survivor. A false flag teaches the
# writer to hedge a claim it had every right to make, so the check is tuned
# for precision.
_ABSENCE_ANGLE_JACCARD = 0.5


def _colliding_angle(sentence: str, angles: Sequence[tuple[str, frozenset[str]]]) -> str | None:
    """The untested query this sentence is talking about, if any.

    Set arithmetic only — no model judgment, and no partial-credit scoring.
    The first angle over the bar wins; angles arrive in the order the run
    issued them, so the finding names the earliest one.
    """
    tokens = query_tokens(sentence)
    if not tokens:
        return None
    for angle, angle_tokens in angles:
        if len(tokens & angle_tokens) / len(tokens | angle_tokens) >= _ABSENCE_ANGLE_JACCARD:
            return angle
    return None


def absence_findings(
    summary: str,
    sections: Sequence[tuple[str, str]],
    untested: Sequence[str],
) -> list[Finding]:
    """Sentences that turn an unreachable angle into a fact about the world.

    Two independent conditions must both hold: the sentence makes a strong
    absence assertion from the frozen set above, AND its content words overlap
    a query whose every issue died in a host outage. A run that tested every
    query it issued produces nothing here at all.
    """
    angles = untested_angle_tokens(untested)
    if not angles:
        return []
    out: list[Finding] = []
    spans: list[tuple[str, str]] = [(SUMMARY_WHERE, summary), *sections]
    for where, text in spans:
        for sentence in _plain_sentences(text):
            if not any(pattern.search(sentence) for pattern in _ABSENCE_ASSERTIONS):
                continue
            angle = _colliding_angle(sentence, angles)
            if angle is None:
                continue
            out.append(
                Finding(
                    where=where,
                    quote=_quote(sentence),
                    fix=(
                        f'This asserts an absence on the angle "{angle}", which this '
                        "research could never reach, so nothing about it could enter "
                        "the evidence. Either cite a source that AFFIRMS the absence, "
                        "or rewrite the sentence as an open question about the subject; "
                        "an absence may never be inferred from evidence that was never "
                        "available."
                    ),
                    kind=KIND_ABSENCE,
                    rubric="R3",
                )
            )
    return out[:_MAX_ABSENCE]


# ---------------------------------------------------------------------------
# Sentences the verifier could not support.
# ---------------------------------------------------------------------------


def unsupported_findings(
    unsupported: Sequence[
        tuple[str, str, Sequence[str]] | tuple[str, str, Sequence[str], Sequence[str]]
    ],
) -> list[Finding]:
    """One finding per sentence the NLI verifier could not support.

    ``unsupported`` rows are ``(where, sentence, cited_ids)`` or ``(where,
    sentence, cited_ids, borrowed_ids)`` in report order. A cited sentence
    failed against what it cites. An uncited one was held against what its
    paragraph cites (``borrowed_ids``) and contradicted it, or sits in a
    paragraph that cites nothing — and the fix names which, so the rework
    knows whether to find the right source or to cite the paragraph at all.
    """
    out: list[Finding] = []
    for row in unsupported[:_MAX_UNSUPPORTED]:
        where, sentence, ids = row[0], row[1], row[2]
        borrowed = row[3] if len(row) > 3 else ()
        if ids:
            cited = ", ".join(f"[[{item}]]" for item in ids)
            fix = (
                f"An automated evidence check flagged a possible conflict ({cited}). "
                "Check the complete sentence against its cited evidence in context. "
                "Retain it if supported; otherwise cite supporting evidence, correct "
                "the assertion, or remove it. The classifier warning alone is not "
                "proof that the sentence is wrong."
            )
        elif borrowed:
            around = ", ".join(f"[[{item}]]" for item in borrowed)
            fix = (
                f"No citation, and the evidence cited around it ({around}) triggered "
                "an automated conflict warning. Check the complete sentence against "
                "that evidence in context. Retain supported synthesis; otherwise "
                "cite supporting evidence, correct the assertion, or remove it. "
                "The classifier warning alone is not proof that the sentence is wrong."
            )
        else:
            fix = (
                "No citation, and this paragraph cites no evidence: cite the "
                "sources this paragraph rests on, or remove the sentence."
            )
        out.append(
            Finding(
                where=where,
                quote=_quote(sentence),
                fix=fix,
                kind=KIND_UNSUPPORTED,
                rubric="R3",
            )
        )
    return out


# ---------------------------------------------------------------------------
# The reviewer's verdict, resolved to parts.
# ---------------------------------------------------------------------------

_RUBRIC_ITEM = re.compile(r"^R([1-9]|10)$")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", CITATION_MARKER.sub("", text)).strip().casefold()


def _resolve_part(
    section: str, where: str, summary: str, sections: Sequence[tuple[str, str]]
) -> str | None:
    """Which part a reviewer failure is about.

    A uniquely located quote takes precedence over a mistaken section heading.
    Otherwise the heading disambiguates matching quotes or locates a finding
    about the whole section. Unplaceable failures are dropped — a rework cannot rewrite "the
    part with this problem" if no part can be found — and counted, so the
    trail shows how often the reviewer's naming missed.
    """
    name = section.strip().strip("#").strip().casefold()
    needle = _normalized(where)
    matches = [
        title
        for title, body in [(SUMMARY_WHERE, summary), *sections]
        if len(needle.split()) >= 3 and needle in _normalized(body)
    ]
    if len(matches) == 1:
        return matches[0]
    if name in {"executive summary", "summary", "overview", "executive overview"}:
        return SUMMARY_WHERE if not matches or SUMMARY_WHERE in matches else None
    for title, _body in sections:
        if title.casefold() == name:
            return title if not matches or title in matches else None
    return None


def _coverage_finding(
    item: Mapping[str, Any], sections: Sequence[tuple[str, str]]
) -> Finding | None:
    title = re.sub(r"^##[ \t]+", "", str(item.get("section") or "").strip()).strip()
    rubric = str(item.get("rubric") or "").strip().upper()
    fix = str(item.get("fix") or "").strip()
    anchor = re.sub(r"^##[ \t]+", "", str(item.get("after") or "").strip()).strip().casefold()
    known = {name.casefold(): name for name, _body in sections}
    known[SUMMARY_WHERE] = SUMMARY_WHERE
    if (
        rubric not in {"R1", "R2"}
        or not title
        or len(title) > 160
        or "\n" in title
        or title.startswith("#")
        or title.casefold() in known
        or anchor not in known
        or not fix
    ):
        return None
    return Finding(
        where=title,
        quote=_quote(str(item.get("where") or "")),
        fix=fix,
        kind=KIND_COVERAGE,
        rubric=rubric,
        after=known[anchor],
    )


def _placed_review_finding(
    item: Mapping[str, Any],
    where: str,
    fix: str,
    summary: str,
    sections: Sequence[tuple[str, str]],
) -> Finding | None:
    part = _resolve_part(str(item.get("section") or ""), where, summary, sections)
    if part is None:
        return None
    rubric = str(item.get("rubric") or "").strip().upper()
    return Finding(
        where=part,
        quote=_quote(where),
        fix=fix or "Fix the quoted text so it meets the rubric.",
        kind=KIND_REVIEW,
        rubric=rubric if _RUBRIC_ITEM.match(rubric) else "",
    )


def _assessment_findings(rows: Sequence[Mapping[str, Any]]) -> list[Finding]:
    """Carry validated negative claim judgments into the same scoped repair list.

    A reviewer need not duplicate an evidence problem in a second JSON array.
    The claim and section came from this draft; the explanation remains the
    reviewer's judgment, never a host assertion that the source proves it.
    """
    return [
        Finding(
            where=SUMMARY_WHERE if row["section_id"] == "summary" else row["section_id"],
            quote=_quote(row["text"]),
            fix=(
                f"Evidence review: {row['reason']} "
                "Check this claim against the retained evidence and correct or qualify "
                "its substance and attribution; adding a citation alone is insufficient."
            ),
            kind=KIND_REVIEW,
            rubric="R3",
        )
        for row in rows
        if row["review_judgment"] in {"unsupported", "unresolved"}
        or row["evidence_standard"] in {"not_met", "uncertain"}
    ]


def _unassessed_nli_findings(
    suspected: Sequence[tuple[str, str, tuple[str, ...], tuple[str, ...]]],
    assessed: Sequence[Mapping[str, Any]],
) -> list[Finding]:
    """Keep unanswered verifier signals in the repair handoff, without judging truth."""
    reviewed = {
        (SUMMARY_WHERE if row["section_id"] == "summary" else row["section_id"], row["text"])
        for row in assessed
    }
    return [
        Finding(
            where=where,
            quote=_quote(sentence),
            fix=(
                "The verifier reported a possible contradiction that the reviewer has not "
                "assessed. Check this claim against "
                + " ".join(f"[[{source_id}]]" for source_id in (cited or borrowed))
                + ". Retain it if the source supports its substance and conditions; otherwise "
                "correct or qualify it. The verifier signal alone does not justify deleting "
                "supported prose."
            ),
            kind=KIND_REVIEW,
            rubric="R3",
        )
        for where, sentence, cited, borrowed in suspected
        if (where, sentence) not in reviewed
    ]


def review_findings(
    payload: Mapping[str, Any],
    summary: str,
    sections: Sequence[tuple[str, str]],
) -> tuple[list[Finding], int]:
    """The reviewer's failures as findings, plus the count it could not place.

    Reads only the shape the review contract promises and tolerates the rest:
    a failure that is not an object, has no fix, or cannot be placed in any
    part contributes nothing but a count.
    """
    raw = payload.get("failures")
    if not isinstance(raw, list):
        return [], 0
    out: list[Finding] = []
    unplaced = 0
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if item.get("action") == "add_section":
            addition = _coverage_finding(item, sections)
            if addition is None or any(
                f.where.casefold() == addition.where.casefold() for f in out
            ):
                unplaced += 1
            else:
                out.append(addition)
            if len(out) == _MAX_REVIEW:
                break
            continue
        fix = str(item.get("fix") or "").strip()
        where = str(item.get("where") or "").strip()
        if not fix and not where:
            continue
        finding = _placed_review_finding(item, where, fix, summary, sections)
        if finding is None:
            unplaced += 1
            continue
        out.append(finding)
        if len(out) == _MAX_REVIEW:
            break
    return out, unplaced


# ---------------------------------------------------------------------------
# Rendering the list the writer reads.
# ---------------------------------------------------------------------------


def named_parts(findings: Sequence[Finding], order: Sequence[str]) -> list[str]:
    """The parts with findings, in report order: the summary first, then the
    sections as they stand. A finding on an unknown part is not named."""
    wanted = {finding.where for finding in findings}
    parts = [SUMMARY_WHERE] if SUMMARY_WHERE in wanted else []
    parts.extend(title for title in order if title in wanted)
    parts.extend(
        finding.where
        for finding in findings
        if finding.kind == KIND_COVERAGE and finding.where not in parts
    )
    return parts


def render_findings(
    findings: Sequence[Finding], order: Sequence[str], *, summary_heading: str
) -> str:
    """The findings grouped under the heading of the part they belong to, in
    report order, numbered once across the whole list."""
    lines: list[str] = []
    number = 0
    for part in named_parts(findings, order):
        heading = summary_heading if part == SUMMARY_WHERE else part
        lines.append(f"## {heading}")
        for finding in findings:
            if finding.where != part:
                continue
            number += 1
            label = f" ({finding.rubric})" if finding.rubric else ""
            quoted = f' "{finding.quote}" —' if finding.quote else ""
            insertion = (
                f"Add this missing section after '{finding.after}'. "
                if finding.kind == KIND_COVERAGE
                else ""
            )
            lines.append(f"{number}.{label}{quoted} {insertion}{finding.fix}")
    return "\n".join(lines)


__all__ = [
    "DIAGNOSTIC_SUMMARY",
    "HOST_VOCABULARY",
    "KIND_ABSENCE",
    "KIND_COVERAGE",
    "KIND_PROCESS",
    "KIND_REPEATED",
    "KIND_REVIEW",
    "KIND_UNRESOLVED_CITATION",
    "KIND_UNSUPPORTED",
    "SUMMARY_WHERE",
    "Finding",
    "absence_findings",
    "named_parts",
    "process_language_findings",
    "render_findings",
    "repetition_findings",
    "review_findings",
    "section_body_text",
    "unresolved_citation_findings",
    "unsupported_findings",
]

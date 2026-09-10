"""The rulers the writer's own review and the acceptance harness both grade with.

A report must never ship carrying a defect the product's own deterministic
review could have measured. That only holds if the two graders read the SAME
ruler: when the harness owns a private copy of a threshold or a normalization,
the product's review can call a draft clean and the acceptance batch can fail
the very same bytes — which is exactly what the final 27B batch produced
(sentence-level repetition, process language, and a thin section all shipped
past a "clean" self-review).

So the normalization, the thresholds, and the vocabulary live here ONCE. The
product's findings pass (`_writer_findings`) builds its review findings from
these functions, and `development/harness/research_harness_parts/_checks.py` imports
them instead of restating them. `development/harness/tests/test_ruler_drift.py`
pins the agreement on fixture reports, the same way slice 6 pinned the
query-identity helpers for the thrash detector.

One ruler is only half the job: the two graders also have to read the same
TEXT. `repetition_verdict`'s paragraph clause was shared here from the start,
yet the product had no way to REPORT it, so `repeated_paragraphs` below exposes
that clause on its own — and the writer grades the report as
`_writer_parts.finalize_report` renders it, which is the artifact the harness
receives.

Nothing in this module produces prompt text or grades a whole report — it only
measures. Naming a finding, deciding what it costs, and deciding what ships are
all somebody else's job.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Citation markers are stripped before ANY measurement: `[[p12]]` is markup, not
# prose, and counting it would make a densely-cited sentence look longer than an
# uncited one. The broad body matches whatever the writer emitted between the
# brackets, so a malformed marker still cannot inflate a word count.
CITATION_MARKER = re.compile(r"\[\[[^\]]+\]\]")
_WORD = re.compile(r"\b[\w'-]+\b")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")

# ---------------------------------------------------------------------------
# Repetition.
# ---------------------------------------------------------------------------

# A fragment shorter than this is punctuation noise ("Fig. 2.", "See below."),
# not a framing sentence a reader would notice twice.
SENTENCE_MIN_WORDS = 5
# Below this, there is not enough prose to judge repetition at all. The harness
# treats that as its own failure (a report this short is not a report); the
# product's review sends a report this short back to the reviewer instead, so
# only the harness verdict reads this bound.
REPETITION_MIN_SENTENCES = 8
# A sentence printed twice is a repeated sentence. The ruler used to tolerate a
# refrain up to four times as long as the repeats stayed under 5% of the body,
# and the phase-6 blind eval scored the reports that tolerance let through at
# the bottom on reads/flows. There is no acceptable amount of a sentence a
# reader has already read; the one carve-out is a table cell, which is a value
# in a grid and not a sentence in the argument.
REPETITION_MAX_REPEAT = 2
# Paragraph-level duplication is measured on paragraphs long enough to carry an
# argument; two identical short list items are not a repeated analysis.
PARAGRAPH_MIN_WORDS = 20
# Table rows carry values, not argument: `| Not reported in the evidence |`
# printed in six rows is a grid, so table lines are left out of the sentence
# clause. Paragraph duplication still sees a whole table pasted twice.
_TABLE_ROW = re.compile(r"^\s*\|")


@dataclass(frozen=True)
class RepeatedText:
    """One repeated sentence or paragraph: its identity and how often it occurs.

    ``quote`` is the FIRST occurrence as the report actually wrote it. The
    comparison key is casefolded, and handing a casefolded sentence back to a
    writer that has to find and delete it is a worse instruction than handing
    back its own words.
    """

    text: str
    count: int
    quote: str


# The name the sentence clause has always been read under.
RepeatedSentence = RepeatedText
# …and the paragraph clause's, so a caller naming a duplicated paragraph reads
# as a paragraph finding rather than borrowing the sentence vocabulary.
RepeatedParagraph = RepeatedText


@dataclass(frozen=True)
class RepetitionVerdict:
    """Everything both graders read about one report's repetition.

    The three failure conditions are kept apart because the two callers need
    different ones: the harness's single boolean is all three OR'd together,
    while the product's review turns only the two repetition conditions into
    findings a writer can act on — a report that is merely short is not a
    repetition problem, and the product asks for no length at all.
    """

    sentence_count: int
    repeated_sentences: tuple[RepeatedText, ...]
    max_repeat: int
    repeated_fraction: float
    repeated_paragraphs: tuple[RepeatedText, ...]

    @property
    def too_little_prose(self) -> bool:
        return self.sentence_count < REPETITION_MIN_SENTENCES

    @property
    def sentences_repeat(self) -> bool:
        return self.max_repeat >= REPETITION_MAX_REPEAT

    @property
    def duplicate_paragraph(self) -> bool:
        """The paragraph clause: one body paragraph printed twice over.

        Identical to the count the harness used to compute privately — a
        paragraph long enough to carry an argument, normalized and casefolded,
        occurring two or more times.
        """
        return bool(self.repeated_paragraphs)

    @property
    def acceptable(self) -> bool:
        return not (self.too_little_prose or self.sentences_repeat or self.duplicate_paragraph)


def word_count(text: str) -> int:
    """Prose words, with citation markup removed first."""
    return len(_WORD.findall(CITATION_MARKER.sub("", text)))


def _plain_sentences(prose: str) -> list[str]:
    """Cited-stripped, whitespace-collapsed sentences in their own case.

    Table rows are dropped before splitting (see ``_TABLE_ROW``)."""
    without_tables = "\n".join(line for line in prose.splitlines() if not _TABLE_ROW.match(line))
    sentences = [
        re.sub(r"\s+", " ", CITATION_MARKER.sub("", sentence)).strip()
        for sentence in _SENTENCE_BREAK.split(without_tables)
    ]
    return [sentence for sentence in sentences if word_count(sentence) >= SENTENCE_MIN_WORDS]


def normalized_sentences(prose: str) -> list[str]:
    """Split into comparable sentences: cited-stripped, whitespace-collapsed,
    casefolded, and dropped when too short to be a framing sentence."""
    return [sentence.lower() for sentence in _plain_sentences(prose)]


def _plain_paragraphs(prose: str) -> list[str]:
    """Cited-stripped, whitespace-collapsed paragraphs in their own case.

    Only paragraphs long enough to carry an argument are kept; two identical
    short list items are not a repeated analysis.
    """
    paragraphs = [
        re.sub(r"\s+", " ", CITATION_MARKER.sub("", paragraph)).strip()
        for paragraph in _PARAGRAPH_BREAK.split(prose)
    ]
    return [paragraph for paragraph in paragraphs if word_count(paragraph) >= PARAGRAPH_MIN_WORDS]


def normalized_paragraphs(prose: str) -> list[str]:
    """Split into comparable paragraphs, the paragraph clause's counterpart to
    `normalized_sentences`: cited-stripped, whitespace-collapsed, casefolded,
    and dropped when too short to carry an argument."""
    return [paragraph.lower() for paragraph in _plain_paragraphs(prose)]


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _repeats(plain: Sequence[str]) -> tuple[dict[str, int], tuple[RepeatedText, ...]]:
    """Occurrence counts by casefolded key, plus the repeated ones in order.

    One helper for both clauses, so "the same sentence" and "the same
    paragraph" can never come to mean two different kinds of sameness.
    """
    keys = [value.lower() for value in plain]
    counts = _counts(keys)
    first_written: dict[str, str] = {}
    for key, original in zip(keys, plain, strict=True):
        first_written.setdefault(key, original)
    return counts, tuple(
        RepeatedText(text=text, count=count, quote=first_written[text])
        for text, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))
        if count > 1
    )


def repeated_paragraphs(prose: str) -> tuple[RepeatedText, ...]:
    """The paragraph clause on its own: body paragraphs printed more than once.

    The harness's ``repetition_acceptable`` fails on this alone, so the
    product's review needs it by itself to name the paragraph to delete.
    """
    return _repeats(_plain_paragraphs(prose))[1]


def repetition_verdict(prose: str) -> RepetitionVerdict:
    """Measure sentence and paragraph repetition across report body prose."""
    plain = _plain_sentences(prose)
    counts, repeated_sentences = _repeats(plain)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return RepetitionVerdict(
        sentence_count=len(plain),
        repeated_sentences=repeated_sentences,
        max_repeat=max(counts.values(), default=0),
        repeated_fraction=repeated / len(plain) if plain else 0.0,
        repeated_paragraphs=repeated_paragraphs(prose),
    )


def repetition_is_acceptable(prose: str) -> bool:
    """The harness's whole-report repetition verdict."""
    return repetition_verdict(prose).acceptable


# ---------------------------------------------------------------------------
# Substantive section bodies.
# ---------------------------------------------------------------------------

# The acceptance floor for one '## ' section's body. A section under it is not a
# section — it is a heading with a gesture beneath it. It was 120 while the
# writer was held to a word floor; without one, a 40-word section is a short
# section and anything under that is a stub the reviewer (rubric R2) names.
SECTION_MIN_WORDS = 40
PUNCTUATION_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
# A placeholder can carry enough repeated punctuation to clear a naive word
# count (`...`, `[content omitted]`), so it is named explicitly.
PLACEHOLDER_BODY = re.compile(r"[\s\W]*(?:tbd|n/?a|omitted|placeholder)[\s\W]*", re.IGNORECASE)


def section_body_is_thin(body: str) -> bool:
    """Whether one section body falls under the substantive-body floor."""
    stripped = body.strip()
    return bool(
        word_count(stripped) < SECTION_MIN_WORDS
        or PUNCTUATION_ONLY.fullmatch(stripped)
        or PLACEHOLDER_BODY.fullmatch(stripped)
    )


# ---------------------------------------------------------------------------
# Process / diagnostic language.
# ---------------------------------------------------------------------------
#
# Rubric R7: the report discusses its SUBJECT, never how the research or its
# checks were performed. Both patterns stay narrow so a report legitimately
# ABOUT retrieval systems or provider outages is not punished for its topic.
#
# References to prompt-local materials are process narration. A statement about
# what supplied evidence establishes is a legitimate evidentiary limitation,
# including when it explains missing coverage. Do not order its removal merely
# because it contains the words "provided evidence" or "does not include".
_POOL_VOCABULARY = (
    r"evidence (?:at hand|before me|in front of me)|"
    r"sources (?:above|at hand|before me|in front of me)|"
    r"the sources (?:supplied|provided|given)(?! (?:by|to|for|that)\b)"
)

DIAGNOSTIC_LANGUAGE_SUMMARY = re.compile(
    r"\b(?:"
    r"research process|retrieval (?:failed|failure|gap|status)|source availability|"
    r"verification (?:failed|failure|status)|"
    r"confidence labels?|confidence scores?|evidence map|subquestions?|"
    r"search queries?|search process|provider response|report structure|"
    r"we searched|we reviewed|we found|could not verify|"
    r"coverage (?:gap|gaps|was)|word count|section count|"
    rf"{_POOL_VOCABULARY}"
    r")\b",
    re.IGNORECASE,
)
PROCESS_LANGUAGE_BODY = re.compile(
    r"\b(?:research process|retrieval (?:failed|failure|gap|status)|"
    r"verification (?:failed|failure|status)|source availability prevented|"
    r"we searched|we reviewed|search queries?|search process|provider response|"
    r"subquestion (?:failed|status)|word count target|section count target|"
    rf"{_POOL_VOCABULARY})\b",
    re.IGNORECASE,
)


def has_process_language(summary: str, body: str) -> bool:
    """The harness's whole-report R7 verdict."""
    return bool(DIAGNOSTIC_LANGUAGE_SUMMARY.search(summary) or PROCESS_LANGUAGE_BODY.search(body))


@dataclass(frozen=True)
class ProcessLanguageHit:
    """One offending sentence and the vocabulary that convicted it."""

    where: str  # "executive summary" or the section title
    sentence: str
    phrase: str


def _hits(where: str, text: str, pattern: re.Pattern[str]) -> list[ProcessLanguageHit]:
    hits: list[ProcessLanguageHit] = []
    for raw in _SENTENCE_BREAK.split(text):
        sentence = re.sub(r"\s+", " ", CITATION_MARKER.sub("", raw)).strip()
        match = pattern.search(sentence)
        if match is not None:
            hits.append(ProcessLanguageHit(where=where, sentence=sentence, phrase=match.group(0)))
    return hits


def process_language_hits(
    summary: str, sections: Sequence[tuple[str, str]]
) -> tuple[ProcessLanguageHit, ...]:
    """Locate the sentences that make the harness's R7 verdict fail.

    The harness answers only "does this report narrate its own process"; a
    review that has to REPAIR the report needs the sentence, so the same two
    patterns are run per sentence here.
    """
    hits = _hits("executive summary", summary, DIAGNOSTIC_LANGUAGE_SUMMARY)
    for title, markdown in sections:
        hits.extend(_hits(title, markdown, PROCESS_LANGUAGE_BODY))
    return tuple(hits)


__all__ = [
    "CITATION_MARKER",
    "DIAGNOSTIC_LANGUAGE_SUMMARY",
    "PARAGRAPH_MIN_WORDS",
    "PLACEHOLDER_BODY",
    "PROCESS_LANGUAGE_BODY",
    "PUNCTUATION_ONLY",
    "REPETITION_MAX_REPEAT",
    "REPETITION_MIN_SENTENCES",
    "SECTION_MIN_WORDS",
    "SENTENCE_MIN_WORDS",
    "ProcessLanguageHit",
    "RepeatedParagraph",
    "RepeatedSentence",
    "RepeatedText",
    "RepetitionVerdict",
    "has_process_language",
    "normalized_paragraphs",
    "normalized_sentences",
    "process_language_hits",
    "repeated_paragraphs",
    "repetition_is_acceptable",
    "repetition_verdict",
    "section_body_is_thin",
    "word_count",
]

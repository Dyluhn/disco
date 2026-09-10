"""A draft sentence claiming the record lacks something, checked against it.

Measured on Q4-GLM. The writer's notes for the Federal Register source quoted
Table II.2's rows with exact offsets, and the report still wrote that the
stringency levels "could not be verified from the admitted text" — citing that
same source, in a sentence that used a figure only those notes could supply.
Nothing in the review had a reason to look: an assertion of absence reads like
caution, and caution is not what a rubric hunts for.

But an assertion of absence is the one claim a program can put evidence in
front of. The sentence names a source; the source either contains the thing or
it does not. So the tagging is deterministic and so is the evidence gathering:
the phrases below identify the sentence, the terms it shares with its cited
sources locate candidate spans through the extraction-tolerant matcher, and
the reviewer is handed those spans and asked to judge. Judgement stays with the
model; finding the contradiction does not.

The candidate spans come from the SOURCE TEXT rather than from the reader's
notes. The source is the authority — every accepted note quotes it, so term
overlap finds the same spans with the same offsets — and a run resumed from a
checkpoint written before notes existed is checked exactly as well.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..models import Passage
from ..source_excerpts import evidence_terms
from ..text_match import locate_all, normalized_form, normalized_index
from .quality_audit import ClaimRecord

#: The ways a report says the record does not contain something. One list, so a
#: new wording is added in one place and the test below proves each one tags.
ABSENCE_PHRASES: tuple[str, ...] = (
    "could not be verified",
    "cannot be verified",
    "could not be confirmed",
    "cannot be confirmed",
    "not in the admitted",
    "not present in the admitted",
    "does not reach",
    "remains open",
    "remain open",
    "is not available in the evidence",
    "not available in the admitted",
    "no source in the pool",
    "unverifiable",
    "the evidence does not establish",
    "the record does not contain",
    "is not stated in the",
    "are not stated in the",
)

#: How many contradicting spans one absence claim is worth showing, and how
#: much of the source each one carries. Bounded like an inspection excerpt: the
#: reviewer needs enough to judge, not the source again.
MAX_CANDIDATE_SPANS = 5
CANDIDATE_CONTEXT_CHARS = 240
#: A term has to be worth searching for. One-character tokens and bare stop
#: words match everywhere and would bury the span that matters.
MIN_TERM_CHARS = 3


def absence_claims(claims: Sequence[ClaimRecord]) -> list[ClaimRecord]:
    """Every claim whose text asserts the record lacks something."""
    return [claim for claim in claims if is_absence_claim(claim.text)]


def is_absence_claim(text: str) -> bool:
    folded = " ".join(text.casefold().split())
    return any(phrase in folded for phrase in ABSENCE_PHRASES)


def _searchable_terms(text: str) -> list[str]:
    """The distinctive words and numbers of the sentence, longest first.

    Longest first because a long term is the one whose presence actually
    settles the question; `evidence_terms` has already dropped the stop words.
    """
    terms = {term for term in evidence_terms(text) if len(term) >= MIN_TERM_CHARS}
    terms -= {phrase.split()[-1] for phrase in ABSENCE_PHRASES}
    return sorted(terms, key=lambda term: (-len(term), term))


def _candidate_spans(passage: Passage, terms: Sequence[str]) -> list[dict[str, object]]:
    """Spans in the source that carry the sentence's own terms, best first.

    A span is ranked by how much of the sentence its surrounding excerpt
    accounts for, not by how early it appears: the row that settles the
    question is the one where the sentence's terms come together.
    """
    index = normalized_index(passage.text, fold=True)
    hits: dict[tuple[int, int], set[str]] = {}
    for term in terms:
        for span in locate_all(term, index, limit=MAX_CANDIDATE_SPANS, fold=True):
            hits.setdefault(span, set()).add(term)
    scored: list[tuple[int, int, tuple[int, int], list[str]]] = []
    for span in hits:
        left = max(0, span[0] - CANDIDATE_CONTEXT_CHARS // 3)
        window = passage.text[left : left + CANDIDATE_CONTEXT_CHARS]
        covered = sorted(
            term
            for term in terms
            if normalized_form(term, fold=True) in normalized_form(window, fold=True)
        )
        scored.append((-len(covered), span[0], span, covered))
    rows: list[dict[str, object]] = []
    for _rank, _start, span, covered in sorted(scored)[:MAX_CANDIDATE_SPANS]:
        left = max(0, span[0] - CANDIDATE_CONTEXT_CHARS // 3)
        rows.append(
            {
                "source_id": passage.id,
                "start": span[0],
                "end": span[1],
                "terms": covered,
                "text": passage.text[left : left + CANDIDATE_CONTEXT_CHARS],
            }
        )
    return rows


def absence_context(claims: Sequence[ClaimRecord], by_id: Mapping[str, Passage]) -> str:
    """What the reviewer reads about every absence assertion in this draft.

    Each claim arrives with the spans its own cited sources hold for its own
    terms, or with the normalised forms that were searched and found nothing.
    A claim whose source does contain the thing is unsupported, and the offset
    is the evidence for saying so.
    """
    if not claims:
        return ""
    rows: list[dict[str, object]] = []
    for claim in claims:
        terms = _searchable_terms(claim.text)
        spans: list[dict[str, object]] = []
        for source_id in claim.source_ids:
            passage = by_id.get(source_id)
            if passage is not None:
                spans.extend(_candidate_spans(passage, terms))
        row: dict[str, object] = {"claim_id": claim.claim_id, "claim": claim.text}
        if spans:
            row["found_in_the_cited_source"] = spans[:MAX_CANDIDATE_SPANS]
        else:
            row["found_in_the_cited_source"] = "no span in the cited source carries these terms"
            row["searched"] = [
                normalized_form(term, fold=True) for term in terms[:MAX_CANDIDATE_SPANS]
            ]
        rows.append(row)
    return (
        "\nABSENCE ASSERTIONS (draft sentences claiming the record lacks something, "
        "with what their OWN cited sources hold for their own terms; offsets are "
        "characters in the source, and the search ignores what page extraction "
        "added — whitespace inside a word, soft hyphens, markdown emphasis). "
        "Judge each one: if the cited source does contain what the sentence says is "
        "missing, the claim is unsupported and the offset below is the evidence. "
        "A sentence whose subject is genuinely absent from the record is correct "
        "and must be preserved:\n" + json.dumps(rows, ensure_ascii=False)
    )


__all__ = [
    "ABSENCE_PHRASES",
    "MAX_CANDIDATE_SPANS",
    "absence_claims",
    "absence_context",
    "is_absence_claim",
]

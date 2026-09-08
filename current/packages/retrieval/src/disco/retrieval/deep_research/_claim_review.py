"""Attach mechanically validated reviewer observations to existing claim records.

Matching source spans establishes what was read, not that a model's semantic
judgment is true. The final draft hash binds these records to the reviewed prose.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from typing import Any

from ..models import Passage
from ._citation_aliases import CitationAliases
from ._report_rulers import CITATION_MARKER
from .quality_audit import ClaimRecord

MAX_EVIDENCE_RANGE_CHARS = 24_000

CLAIM_REVIEW_INSTRUCTION = (
    '\nEVIDENCE REVIEW: include "checks": [ {"claim_id": "ID from DRAFT CLAIMS", '
    '"judgment": "supported|qualified|unsupported|unresolved", '
    '"evidence_standard": "met|not_met|uncertain", '
    '"evidence": [{"source_id": "s1", "start": 0, "end": 1200}], '
    '"reason": "why that evidence supports this claim at the requested standard"} ]. '
    "Each claim's evidence list must contain at most 4 source spans; use at least one "
    "span for a supported judgment and zero to four spans for other judgments. "
    "Assess the consequential claims that drive the answer across the whole report. "
    "Include quantities, recommendations, standards identifiers and any claim of exclusivity "
    "or absence on which the conclusion depends. Check substance before presentation. "
    "For evidence, copy source_id and the integer start/end range from the relevant "
    "source excerpt or inspection record. These are character offsets, not line numbers. "
    "A range identifies the exact retained text you assessed; it does not establish support "
    "by itself. Explain the supporting or limiting substance in reason. An exact source "
    "quote of at most 600 characters is also accepted (whitespace may be normalized). "
    "Select the claim_id from DRAFT CLAIMS and assess its complete text, including "
    "later clauses and qualifications. Do not substitute a source sentence for a draft claim. "
    "Legacy claim (an exact draft quote, at most 600 characters) is also accepted. "
    "Read beyond an excerpt "
    "when its conditions are unclear, using inspect before returning a verdict. "
    "Evidence standard is claim-relative: a primary announcement establishes the announcement, "
    "not observed performance; a secondary account cannot verify an explicit request for "
    "primary evidence. Explain those distinctions in reason. A supported claim needs evidence; "
    "unavailable evidence means unresolved. Any unsupported or unresolved claim, or unmet "
    "evidence requirement, needs a failure with the specific correction or qualification. "
    "Do not invent a source quote or replace evidence judgment with citation counting."
)


def claim_review_records(
    payload: dict[str, Any] | None,
    claims: list[ClaimRecord],
    by_id: dict[str, Passage],
    aliases: CitationAliases,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not claims:
        return [], []
    checks = payload.get("checks") if payload is not None else None
    if not isinstance(checks, list):
        return [], [
            'Missing or invalid evidence assessments: provide a "checks" array '
            "with at least one distinct draft claim assessment."
        ]
    if not 1 <= len(checks) <= len(claims):
        return [], [
            f"The checks array contains {len(checks)} assessments; "
            f"Available draft claims: {len(claims)}. "
            "Return at least one assessment, choosing distinct claims from DRAFT CLAIMS."
        ]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, check in enumerate(checks, 1):
        try:
            rows.append(_claim_row(check, claims, by_id, aliases))
        except ValueError as exc:
            errors.append(f"Check {index} ({_check_identity(check)}) failed: {exc}")
    if len({(row["claim_id"], row["reviewed_claim"]) for row in rows}) != len(rows):
        errors.append("The reviewer repeated a claim assessment.")
    if payload is not None and payload.get("passes") is True:
        if conflict := _passing_conflict(rows):
            errors.append(conflict)
    return rows, errors


def _check_identity(check: Any) -> str:
    """Identify a malformed check without echoing an unbounded model payload."""
    if not isinstance(check, dict):
        return f"observed type {type(check).__name__}"
    claim_id = check.get("claim_id")
    if isinstance(claim_id, str) and claim_id.strip():
        return f"claim_id={claim_id[:120]!r}"
    claim = check.get("claim")
    if isinstance(claim, str) and claim.strip():
        return f"claim quote={' '.join(claim.split())[:180]!r}"
    return "claim identity missing"


def _passing_conflict(rows: list[dict[str, Any]]) -> str | None:
    """Name assessments that conflict with the aggregate passing verdict."""
    conflicts = [
        row
        for row in rows
        if row["review_judgment"] in {"unsupported", "unresolved"}
        or row["evidence_standard"] in {"not_met", "uncertain"}
    ]
    if conflicts:
        details = "; ".join(
            f"{row['claim_id']} {row['reviewed_claim'][:180]!r} "
            f"(judgment={row['review_judgment']}, "
            f"evidence_standard={row['evidence_standard']}, "
            f"section={row['section_id']})"
            for row in conflicts
        )
        return (
            "A passing verdict conflicts with these evidence assessments: "
            + details
            + ". Correct each assessment from the retained evidence, or return "
            "passes=false with a specific failure and correction or qualification."
        )
    return None


def _claim_row(
    check: Any,
    claims: list[ClaimRecord],
    by_id: dict[str, Passage],
    aliases: CitationAliases,
) -> dict[str, Any]:
    if not isinstance(check, dict):
        raise ValueError("A claim assessment was not an object.")
    if "claim_id" in check:
        matched = next(
            (
                claim
                for index, claim in enumerate(claims, 1)
                if check["claim_id"] in {claim.claim_id, f"c{index}"}
            ),
            None,
        )
        if matched is None:
            raise ValueError(
                "Unknown draft claim_id; choose an ID from DRAFT CLAIMS for this draft."
            )
        if "claim" in check:
            _match_claim(check["claim"], [matched], aliases)
    else:
        matched = _match_claim(check.get("claim"), claims, aliases)
    judgment, standard = _judgments(check)
    spans = _evidence_spans(check.get("evidence"), by_id, aliases, judgment)
    reason = check.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1500:
        raise ValueError("A claim assessment lacked a bounded explanation.")
    record = replace(
        matched,
        review_judgment=judgment,
        evidence_standard=standard,
        reviewed_evidence=tuple(spans),
    )
    return {
        **asdict(record),
        "source_ids": list(record.source_ids),
        "reviewed_evidence": [list(span) for span in record.reviewed_evidence],
        "reviewed_claim": check.get("claim", matched.text),
        "reason": reason,
    }


def _match_claim(quote: Any, claims: list[ClaimRecord], aliases: CitationAliases) -> ClaimRecord:
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 600:
        raise ValueError("A claim assessment lacked a bounded draft quote.")
    quote = " ".join(CITATION_MARKER.sub("", aliases.to_canonical(quote)).split()).rstrip(".")
    matched = [claim for claim in claims if quote in " ".join(claim.text.split())]
    if not matched:
        closest = sorted(
            claims, key=lambda claim: SequenceMatcher(None, quote, claim.text).ratio(), reverse=True
        )[:2]
        choices = [claim.text[:600] for claim in closest]
        raise ValueError(
            f"Draft quote {quote[:180]!r} could not be matched to the reviewed draft. "
            "Copy a contiguous quote exactly, including punctuation, from one factual sentence. "
            f"Closest retained draft claims: {choices!r}"
        )
    return matched[0]


def _judgments(check: dict[str, Any]) -> tuple[str, str]:
    judgment, standard = check.get("judgment"), check.get("evidence_standard")
    if (
        not isinstance(judgment, str)
        or not isinstance(standard, str)
        or judgment not in {"supported", "qualified", "unsupported", "unresolved"}
        or standard
        not in {
            "met",
            "not_met",
            "uncertain",
        }
    ):
        raise ValueError("A claim assessment lacked a support or evidence-standard judgment.")
    return judgment, standard


def _evidence_spans(
    evidence: Any, by_id: dict[str, Passage], aliases: CitationAliases, judgment: str
) -> list[tuple[str, int, int, str]]:
    if not isinstance(evidence, list):
        raise ValueError(
            "Evidence must be a list of 0-4 source span objects; "
            f"observed type {type(evidence).__name__}."
        )
    if len(evidence) > 4:
        raise ValueError(f"Evidence must contain at most 4 source spans; observed {len(evidence)}.")
    if judgment == "supported" and not evidence:
        raise ValueError(
            "A supported claim needs 1-4 source spans; observed 0. "
            "Other judgments may use 0-4 spans."
        )
    spans: list[tuple[str, int, int, str]] = []
    for index, item in enumerate(evidence, 1):
        if not isinstance(item, dict):
            raise ValueError(
                f"Evidence span {index} must be an object; observed type {type(item).__name__}."
            )
        source_id = item.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError("A reviewed evidence span lacked an admitted source ID.")
        source_id = aliases.passage_by_alias.get(source_id.casefold(), source_id)
        source = by_id.get(source_id)
        if source is None:
            raise ValueError(
                f"Evidence source {source_id[:64]} is not admitted; use a listed source ID."
            )
        start, end = _source_range(item, source)
        spans.append((source_id, start, end, hashlib.sha256(source.text.encode()).hexdigest()))
    return spans


def _source_range(item: dict[str, Any], source: Passage) -> tuple[int, int]:
    """Resolve a copied excerpt range or legacy quote against immutable source text.

    Each copied range fits the evidence-range allowance; inspection
    windows remain capped at 2200 characters. A range identifies source content,
    never a host judgment of semantic support.
    """
    start, end = 0, len(source.text)
    has_range = "start" in item or "end" in item
    if has_range:
        start, end = item.get("start"), item.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(source.text)
            or end - start > MAX_EVIDENCE_RANGE_CHARS
        ):
            raise ValueError(
                f"Invalid evidence range for source {source.id[:64]!r}: "
                f"observed start={_bounded_repr(start)}, end={_bounded_repr(end)}; "
                f"expected integer start/end with 0 <= start < end <= {len(source.text)} "
                "and a span of at most 24000 "
                "characters."
            )
        if "quote" not in item:
            return start, end
    text = item.get("quote")
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise ValueError("A reviewed evidence span lacked a source range or bounded exact quote.")
    matched = re.search(
        r"\s+".join(re.escape(part) for part in text.split()), source.text[start:end]
    )
    if matched is None:
        raise ValueError(
            f"Evidence quote for {source.id[:64]} was not found in the selected source range. "
            "Copy its excerpt start/end, or a contiguous exact quote; do not paraphrase "
            "or combine separated spans."
        )
    return start + matched.start(), start + matched.end()


def _bounded_repr(value: Any, limit: int = 80) -> str:
    rendered = repr(value)
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."

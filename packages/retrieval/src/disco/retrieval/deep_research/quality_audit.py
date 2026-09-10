"""Small, deterministic quality diagnostics for deep-research reports.

The module deliberately has no dependency on report or source implementation
details.  Callers adapt their claim ledger to :class:`ClaimRecord` and provide
one stable source-to-work function (for example, a DOI/canonical-URL lookup).
Diagnostics are advisory and are suitable as inputs to an existing repair
prompt; this module never edits report prose or calls a model.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit

SourceKey = str
WorkResolver = Callable[[SourceKey], str]


@dataclass(frozen=True)
class ClaimRecord:
    """Minimal claim shape needed by the audits.

    ``source_ids`` may be passage IDs or URLs.  A resolver maps each one to a
    canonical work identity, allowing several passages from one work to count
    only once for corroboration.
    """

    claim_id: str
    text: str
    source_ids: tuple[SourceKey, ...] = ()
    section_id: str = ""
    review_judgment: str = ""
    evidence_standard: str = ""
    reviewed_evidence: tuple[tuple[str, int, int, str], ...] = ()


@dataclass(frozen=True)
class SpecificClaimFinding:
    claim_id: str
    text: str
    matched_rules: tuple[str, ...]
    work_count: int
    domain_count: int
    needs_corroboration: bool


@dataclass(frozen=True)
class Corroboration:
    claim_id: str
    work_count: int
    domain_count: int
    works: tuple[str, ...]
    domains: tuple[str, ...]


@dataclass(frozen=True)
class SourceConcentration:
    total_claims: int
    claims_with_sources: int
    work_claim_counts: tuple[tuple[str, int], ...]
    dominant_work: str | None
    dominant_share: float
    flagged: bool


@dataclass(frozen=True)
class DuplicateParagraph:
    first_index: int
    second_index: int
    similarity: float
    first_excerpt: str
    second_excerpt: str


@dataclass(frozen=True)
class HedgeMetrics:
    paragraph_count: int
    hedge_phrase_count: int
    hedge_density: float
    repeated_phrases: tuple[tuple[str, int], ...]


_SPECIFICITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("percentage", re.compile(r"(?<!\w)\d+(?:\.\d+)?\s*%|\bpercent(?:age)?\b", re.I)),
    ("currency", re.compile(r"(?:[$€£¥]\s*\d|\b\d[\d,.]*\s*(?:USD|EUR|GBP|dollars?)\b)", re.I)),
    ("number", re.compile(r"(?<!\w)\d[\d,.]*(?:\s*(?:million|billion|thousand))?\b", re.I)),
    ("exact_version", re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,2}(?:[-+][\w.-]+)?\b", re.I)),
    (
        "superlative",
        re.compile(r"\b(?:first|only|largest|smallest|highest|lowest|record|never|always)\b", re.I),
    ),
)


def _keys(
    claim: ClaimRecord, resolver: WorkResolver, domain: Callable[[SourceKey], str]
) -> Corroboration:
    works = tuple(sorted({key for source in claim.source_ids if (key := resolver(source))}))
    domains = tuple(sorted({key for source in claim.source_ids if (key := domain(source))}))
    return Corroboration(claim.claim_id, len(works), len(domains), works, domains)


def corroboration_counts(
    claims: Iterable[ClaimRecord],
    work_for_source: WorkResolver,
    domain_for_source: Callable[[SourceKey], str] | None = None,
) -> tuple[Corroboration, ...]:
    """Return distinct canonical-work and domain counts for each claim."""
    domain = domain_for_source or _domain_from_source
    return tuple(_keys(claim, work_for_source, domain) for claim in claims)


def find_high_specificity_claims(
    claims: Iterable[ClaimRecord],
    work_for_source: WorkResolver,
    domain_for_source: Callable[[SourceKey], str] | None = None,
) -> tuple[SpecificClaimFinding, ...]:
    """Find precise claims that deserve corroboration or explicit attribution."""
    domain = domain_for_source or _domain_from_source
    findings: list[SpecificClaimFinding] = []
    for claim in claims:
        matched = tuple(name for name, pattern in _SPECIFICITY_RULES if pattern.search(claim.text))
        if matched:
            corr = _keys(claim, work_for_source, domain)
            findings.append(
                SpecificClaimFinding(
                    claim.claim_id,
                    claim.text,
                    matched,
                    corr.work_count,
                    corr.domain_count,
                    corr.work_count < 2,
                )
            )
    return tuple(findings)


def source_concentration(
    claims: Iterable[ClaimRecord], work_for_source: WorkResolver, *, threshold: float = 0.15
) -> SourceConcentration:
    """Measure claim share by canonical work (not citation-marker frequency)."""
    records = tuple(claims)
    counts: Counter[str] = Counter()
    for claim in records:
        works = set(filter(None, (work_for_source(source) for source in claim.source_ids)))
        for work in works:
            counts[work] += 1
    total = len(records)
    dominant, dominant_count = counts.most_common(1)[0] if counts else (None, 0)
    share = dominant_count / total if total else 0.0
    # A concentration warning is meaningful only when there are at least two
    # claims; a single authoritative claim should not be called a deficiency.
    return SourceConcentration(
        total,
        sum(bool(c.source_ids) for c in records),
        tuple(sorted(counts.items())),
        dominant,
        share,
        total > 1 and share >= threshold,
    )


def near_duplicate_body_paragraphs(
    summary: str, body_paragraphs: Sequence[str], *, threshold: float = 0.88
) -> tuple[DuplicateParagraph, ...]:
    """Find exact/near duplicate body paragraphs; the summary is never compared."""
    del summary  # Explicit API reminds callers that executive summary is excluded.
    normalized = [
        value if len(value.split()) >= 20 else ""
        for paragraph in body_paragraphs
        for value in [_normalize_prose(paragraph)]
    ]
    findings: list[DuplicateParagraph] = []
    for left in range(len(normalized)):
        if not normalized[left]:
            continue
        for right in range(left + 1, len(normalized)):
            if not normalized[right]:
                continue
            similarity = SequenceMatcher(None, normalized[left], normalized[right]).ratio()
            if similarity >= threshold:
                findings.append(
                    DuplicateParagraph(
                        left,
                        right,
                        round(similarity, 4),
                        body_paragraphs[left][:160],
                        body_paragraphs[right][:160],
                    )
                )
    return tuple(findings)


_HEDGE_PHRASES = (
    "may",
    "might",
    "could",
    "likely",
    "appears to",
    "suggests that",
    "it is unclear",
    "evidence is mixed",
)


def hedge_boilerplate_metrics(paragraphs: Iterable[str]) -> HedgeMetrics:
    """Count hedge language and repeated hedge phrases for editorial review."""
    values = tuple(paragraphs)
    counts: Counter[str] = Counter()
    for paragraph in values:
        lower = paragraph.lower()
        for phrase in _HEDGE_PHRASES:
            counts[phrase] += len(re.findall(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", lower))
    repeated = tuple(sorted((phrase, count) for phrase, count in counts.items() if count >= 2))
    total = sum(counts.values())
    return HedgeMetrics(len(values), total, total / len(values) if values else 0.0, repeated)


def _normalize_prose(value: str) -> str:
    value = re.sub(r"\[\[[^]]+\]\]", "", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def _domain_from_source(source: SourceKey) -> str:
    parsed = urlsplit(source if "://" in source else "https://" + source)
    return parsed.netloc.lower().removeprefix("www.")

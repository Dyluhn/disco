"""Deterministic report-quality metrics for acceptance runs.

These are observations, not new publication gates.  They expose the source-
identity and prose signals used by the existing writer review so repeated
batch runs can be compared without an LLM judge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from disco.retrieval.deep_research.quality_audit import (
    ClaimRecord,
    find_high_specificity_claims,
    hedge_boilerplate_metrics,
    near_duplicate_body_paragraphs,
    source_concentration,
)
from disco.retrieval.deep_research.source_identity import canonical_work_key


def report_quality_metrics(report: Mapping[str, Any]) -> dict[str, Any]:
    passages = [
        item for item in report.get("passages", []) if isinstance(item, Mapping)
    ]
    by_id = {
        str(item.get("id")): item
        for item in passages
        if str(item.get("id") or "").strip()
    }

    def work_for_source(source_id: str) -> str:
        source = by_id.get(source_id)
        return canonical_work_key(source) if source is not None else ""

    def domain_for_source(source_id: str) -> str:
        source = by_id.get(source_id)
        if source is None:
            return ""
        url = str(source.get("source_url") or source.get("url") or "")
        return urlsplit(url).netloc.lower().removeprefix("www.")

    claims = _claim_records(report)
    specifics = find_high_specificity_claims(claims, work_for_source, domain_for_source)
    concentration = source_concentration(claims, work_for_source, threshold=0.15)
    paragraphs = _body_paragraphs(report)
    duplicates = near_duplicate_body_paragraphs(
        str(report.get("summary") or ""), paragraphs
    )
    hedge = hedge_boilerplate_metrics(paragraphs)
    works = {canonical_work_key(item) for item in passages}
    works.discard("url:")
    return {
        "passage_count": len(passages),
        "distinct_work_count": len(works),
        "claim_count": len(claims),
        "high_specificity_claims": len(specifics),
        "single_work_specific_claims": sum(
            finding.needs_corroboration for finding in specifics
        ),
        "dominant_work_claim_share": round(concentration.dominant_share, 4),
        "source_concentration_flagged": concentration.flagged,
        "near_duplicate_body_paragraphs": len(duplicates),
        "hedge_phrase_count": hedge.hedge_phrase_count,
        "repeated_hedge_phrases": dict(hedge.repeated_phrases),
    }


def _claim_records(report: Mapping[str, Any]) -> list[ClaimRecord]:
    records: list[ClaimRecord] = []
    for index, raw in enumerate(report.get("claims", [])):
        if not isinstance(raw, Mapping):
            continue
        claim = raw.get("claim")
        if not isinstance(claim, Mapping):
            continue
        ids = claim.get("cited_passage_ids")
        source_ids = tuple(str(item) for item in ids) if isinstance(ids, list) else ()
        records.append(
            ClaimRecord(
                claim_id=str(raw.get("id") or index),
                text=str(claim.get("text") or ""),
                source_ids=source_ids,
                section_id=str(raw.get("section_id") or ""),
            )
        )
    return records


def _body_paragraphs(report: Mapping[str, Any]) -> list[str]:
    return [
        paragraph.strip()
        for section in report.get("sections", [])
        if isinstance(section, Mapping)
        for paragraph in re.split(r"\n\s*\n", str(section.get("markdown") or ""))
        if paragraph.strip()
    ]


__all__ = ["report_quality_metrics"]

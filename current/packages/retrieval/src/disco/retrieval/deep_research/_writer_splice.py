"""Validate and splice whole report parts without discarding unapplied analysis."""

from __future__ import annotations

from collections.abc import Sequence

from ._writer_findings import SUMMARY_WHERE
from ._writer_parts import (
    SUMMARY_SECTION_TITLES,
    FinalReport,
    cited_ids,
    count_prose_words,
    finalize_report,
    normalize_citations,
    parse_report_parts,
)


def _not_a_rewrite(original: str, returned: str, pool_ids: set[str]) -> str | None:
    """Why a returned part cannot stand in for the part it was matched to.

    The report's own rule is that every factual claim carries a citation, so
    a part that came back with none — where the part it replaces carries
    them — is not a rewrite of that part. Observed live: a rework re-ask that
    answered with one ten-word fragment and no citation, which the splice then
    shipped as the executive summary. Bare ``[id]`` markers are promoted first,
    exactly as the shipped text promotes them, so a rewrite that cited in
    single brackets is not mistaken for one that did not cite. A part whose
    original carried no citation is never held to a rule it never met.
    """
    returned_ids = cited_ids(normalize_citations(returned, pool_ids))
    unknown = sorted(set(returned_ids) - pool_ids)
    if unknown:
        return "unknown citation ids: " + ", ".join(f"[[{item}]]" for item in unknown)
    if not cited_ids(original):
        return None
    if returned_ids:
        return None
    return (
        f"{count_prose_words(returned)} words and no [[id]] citation, where the "
        f"original carries {len(cited_ids(original))}"
    )


def _returned_parts(
    rework: str, named: Sequence[str]
) -> tuple[str, list[tuple[str, str]], list[str]]:
    parsed = parse_report_parts(rework, lift_summary_heading=False)
    named_keys = {part.casefold() for part in named if part != SUMMARY_WHERE}
    sections: list[tuple[str, str]] = []
    headed_summary = ""
    for title, body in parsed.sections:
        if not body.strip():
            continue
        if (
            not headed_summary
            and title.casefold() not in named_keys
            and title.casefold() in SUMMARY_SECTION_TITLES
        ):
            headed_summary = body.strip()
        else:
            sections.append((title, body))
    return headed_summary or parsed.summary.strip(), sections, [title for title, _body in sections]


def _candidate_parts(
    sections: Sequence[tuple[str, str]],
    named: Sequence[str],
    *,
    fixed_titles: frozenset[str] = frozenset(),
) -> dict[str, str]:
    by_title = {title.casefold(): body for title, body in sections}
    names = [part for part in named if part != SUMMARY_WHERE]
    candidates = {
        title: by_title[title.casefold()] for title in names if title.casefold() in by_title
    }
    unmatched_names = [title for title in names if title not in candidates]
    matched_keys = {title.casefold() for title in candidates}
    unmatched_returned = [
        (title, body) for title, body in sections if title.casefold() not in matched_keys
    ]
    if (
        len(unmatched_names) == 1
        and len(unmatched_returned) == 1
        and unmatched_names[0] not in fixed_titles
    ):
        candidates[unmatched_names[0]] = unmatched_returned[0][1]
    return candidates


def _splice_candidates(
    final: FinalReport,
    candidates: dict[str, str],
    pool_ids: set[str],
    applied: list[str],
    rejected: dict[str, str],
) -> dict[str, str]:
    originals = dict(final.sections)
    replacements: dict[str, str] = {}
    for title, body in candidates.items():
        reason = _not_a_rewrite(originals.get(title, ""), body, pool_ids)
        if (
            reason is None
            and title not in originals
            and not cited_ids(normalize_citations(body, pool_ids))
        ):
            reason = "a missing-coverage section needs a citation to the admitted evidence"
        if reason is None:
            replacements[title] = body
            applied.append(title)
        else:
            rejected[title] = reason
    return replacements


def _preserve_summary_until_additions(
    summary: str,
    original: str,
    additions: dict[str, str],
    replacements: dict[str, str],
    applied: list[str],
    rejected: dict[str, str],
) -> str:
    missing = [
        title
        for title, anchor in additions.items()
        if anchor == SUMMARY_WHERE and title not in replacements
    ]
    if not missing or SUMMARY_WHERE not in applied:
        return summary
    # The requested body may contain analysis being moved out of the summary.
    # Keep the original until every new body part at that boundary was accepted.
    applied.remove(SUMMARY_WHERE)
    rejected[SUMMARY_WHERE] = "required body additions were not applied: " + ", ".join(missing)
    return original


def _splice_rework(
    final: FinalReport,
    rework: str,
    named: Sequence[str],
    pool_ids: set[str],
    *,
    additions: dict[str, str] | None = None,
) -> tuple[FinalReport, list[str], list[str], dict[str, str]]:
    additions = additions or {}
    returned_summary, returned_sections, returned = _returned_parts(rework, named)
    applied: list[str] = []
    rejected: dict[str, str] = {}
    summary = final.summary
    if returned_summary:
        if SUMMARY_WHERE in named:
            reason = _not_a_rewrite(final.summary, returned_summary, pool_ids)
            if reason is None:
                summary = returned_summary
                applied.append(SUMMARY_WHERE)
            else:
                rejected[SUMMARY_WHERE] = reason
        returned = [SUMMARY_WHERE, *returned]
    replacements = _splice_candidates(
        final,
        _candidate_parts(returned_sections, named, fixed_titles=frozenset(additions)),
        pool_ids,
        applied,
        rejected,
    )
    summary = _preserve_summary_until_additions(
        summary, final.summary, additions, replacements, applied, rejected
    )
    sections: list[tuple[str, str]] = []
    for anchor, body in [(SUMMARY_WHERE, summary), *final.sections]:
        if anchor != SUMMARY_WHERE:
            sections.append((anchor, replacements.get(anchor, body)))
        sections.extend(
            (title, replacements[title])
            for title, after in additions.items()
            if after == anchor and title in replacements
        )
    spliced = FinalReport(title=final.title, summary=summary, sections=tuple(sections))
    return finalize_report(spliced.markdown, pool_ids), returned, applied, rejected


def _complete_rework(
    final: FinalReport,
    rework: str,
    named: Sequence[str],
    pool_ids: set[str],
    *,
    additions: dict[str, str] | None = None,
) -> tuple[FinalReport, list[str], list[str], dict[str, str]]:
    """Accept the requested replacement together, or retain the original draft.

    A cited prefix can contain valid parts while omitting the rest of the
    requested repair. Heading and citation checks establish structural coverage,
    not semantic correctness; the review still owns the latter.
    """
    spliced, returned, applied, rejected = _splice_rework(
        final, rework, named, pool_ids, additions=additions
    )
    missing = [part for part in named if part not in applied]
    if not missing:
        return spliced, returned, applied, rejected
    for part in missing:
        rejected.setdefault(part, "requested part was not returned")
    return final, returned, [], rejected

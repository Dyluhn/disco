"""Report event validation for search oracle."""

from __future__ import annotations

from typing import Any

from ._validators import (
    _CITATION,
    _CONFIDENCE,
    Findings,
    _cited_passage_ids,
    _finish,
    _list,
    _passage_index,
    _text,
    _validate_claims,
    _validate_discovery,
    _validate_ids,
)


def _validate_one_section(
    section: Any,
    index: int,
    *,
    by_id: dict[str, dict[str, Any]],
    section_ids: set[str],
    findings: Findings,
) -> int:
    """Validate one report section. Returns its unsupported count."""
    path = f"sections[{index}]"
    if not isinstance(section, dict):
        findings.add("BAD_REPORT_SECTION", path, "section is not an object")
        return 0
    section_id = _text(section.get("id"))
    if not section_id or section_id in section_ids:
        findings.add("BAD_SECTION_ID", f"{path}.id", "section id is empty or duplicated")
    section_ids.add(section_id)
    findings.require(bool(_text(section.get("title"))), "EMPTY_SECTION_TITLE", path, "empty title")
    markdown = _text(section.get("markdown"))
    findings.require(
        len(markdown) >= 40,
        "EMPTY_SECTION",
        f"{path}.markdown",
        "section body is missing or too short",
    )
    cited = _validate_ids(
        section.get("cited_passage_ids"),
        by_id=by_id,
        findings=findings,
        path=f"{path}.cited_passage_ids",
    )
    markers = set(_CITATION.findall(markdown))
    findings.require(bool(cited), "UNCITED_SECTION", path, "section has no cited passages")
    findings.require(bool(markers), "NO_SECTION_MARKERS", path, "section has no inline citations")
    for passage_id in markers:
        if passage_id not in by_id:
            findings.add("UNRESOLVED_INLINE_CITATION", path, passage_id)
        if passage_id not in cited:
            findings.add("UNDECLARED_INLINE_CITATION", path, passage_id)
    for passage_id in cited:
        if passage_id not in markers:
            findings.add("UNRENDERED_SECTION_CITATION", path, passage_id)
    confidence = section.get("confidence")
    findings.require(
        confidence in _CONFIDENCE,
        "BAD_CONFIDENCE",
        f"{path}.confidence",
        f"unknown confidence {confidence!r}",
    )
    return _validate_section_unsupported(section, path, findings)


def _validate_section_unsupported(
    section: dict[str, Any],
    path: str,
    findings: Findings,
) -> int:
    """Validate and return a section's unsupported_count."""
    section_unsupported = section.get("unsupported_count")
    if isinstance(section_unsupported, int) and not isinstance(section_unsupported, bool):
        findings.require(
            section_unsupported >= 0,
            "BAD_UNSUPPORTED_COUNT",
            f"{path}.unsupported_count",
            "count is negative",
        )
        return max(0, section_unsupported)
    findings.add("BAD_UNSUPPORTED_COUNT", f"{path}.unsupported_count", "count is not an integer")
    return 0


def validate_report_event(
    report: Any,
    *,
    require_web: bool = True,
    connectivity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings = Findings()
    if not isinstance(report, dict):
        findings.add("BAD_REPORT", "$", "report event is not an object")
        return _finish(findings, {})
    findings.require(bool(_text(report.get("query"))), "EMPTY_QUERY", "query", "query is empty")
    findings.require(
        len(_text(report.get("summary"))) >= 40,
        "EMPTY_SUMMARY",
        "summary",
        "report summary is missing or too short",
    )
    by_id = _passage_index(report, findings, require_web=require_web)
    _validate_discovery(report, findings, require_web=require_web)
    sections = _list(report.get("sections"))
    findings.require(bool(sections), "NO_REPORT_SECTIONS", "sections", "report has no sections")
    section_ids: set[str] = set()
    unsupported = 0
    for index, section in enumerate(sections):
        unsupported += _validate_one_section(
            section, index, by_id=by_id, section_ids=section_ids, findings=findings
        )
    findings.require(
        report.get("unsupported_count") == unsupported,
        "UNSUPPORTED_COUNT_MISMATCH",
        "unsupported_count",
        f"declared {report.get('unsupported_count')!r}; sections total {unsupported}",
    )
    claims = report.get("claims")
    if claims is not None:
        _validate_claims(claims, by_id=by_id, findings=findings, required=False)
    if connectivity is not None:
        from ._connectivity import validate_connectivity

        validate_connectivity(connectivity, by_id, _cited_passage_ids(report), findings)
    return _finish(
        findings,
        {"passages": len(by_id), "sections": len(sections), "unsupported": unsupported},
    )

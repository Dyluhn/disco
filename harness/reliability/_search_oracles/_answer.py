"""Grounded answer validation for search oracle."""

from __future__ import annotations

from typing import Any

from ._connectivity import validate_connectivity
from ._validators import (
    Findings,
    _cited_passage_ids,
    _finish,
    _list,
    _passage_index,
    _text,
    _validate_blocks,
    _validate_claims,
    _validate_discovery,
)


def validate_grounded_answer(
    answer: Any,
    *,
    require_web: bool = True,
    connectivity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    findings = Findings()
    if not isinstance(answer, dict):
        findings.add("BAD_ANSWER", "$", "grounded answer is not an object")
        return _finish(findings, {})
    findings.require(bool(_text(answer.get("query"))), "EMPTY_QUERY", "query", "query is empty")
    by_id = _passage_index(answer, findings, require_web=require_web)
    unsupported = _validate_claims(answer.get("claims"), by_id=by_id, findings=findings)
    declared_unsupported = answer.get("unsupported_count")
    findings.require(
        isinstance(declared_unsupported, int)
        and not isinstance(declared_unsupported, bool)
        and declared_unsupported == unsupported,
        "UNSUPPORTED_COUNT_MISMATCH",
        "unsupported_count",
        f"declared {declared_unsupported!r}; graded claims contain {unsupported}",
    )
    _validate_blocks(answer.get("blocks"), by_id=by_id, findings=findings)
    _validate_discovery(answer, findings, require_web=require_web)
    follow_ups = _list(answer.get("follow_ups"))
    valid_followups = [item for item in follow_ups if len(_text(item)) >= 8]
    findings.require(
        len(valid_followups) >= 2,
        "MISSING_FOLLOW_UPS",
        "follow_ups",
        "fewer than two useful follow-up questions were returned",
    )
    if len(valid_followups) != len(set(valid_followups)):
        findings.add("DUPLICATE_FOLLOW_UP", "follow_ups", "follow-up questions repeat")
    if connectivity is not None:
        validate_connectivity(connectivity, by_id, _cited_passage_ids(answer), findings)
    return _finish(
        findings,
        {
            "passages": len(by_id),
            "claims": len(_list(answer.get("claims"))),
            "unsupported": unsupported,
            "follow_ups": len(valid_followups),
        },
    )

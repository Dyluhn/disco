"""Shared evidence-slicing + strict type guards for the P8D edit oracles.

The contract (gpt-5.5): a slice that is ABSENT → SKIP; a slice that is PRESENT but malformed
(not a dict, or a field of the wrong type) → FAIL EDIT_ORACLE_EVIDENCE_MALFORMED (→ INVALID_RUN).
Never coerce a wrong-typed value into a pass. `bool` is NOT accepted as an int/number.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing

ABSENT = object()  # the slice key is not present at all → SKIP
MALFORMED = object()  # the slice key is present but is not a dict → FAIL


def slice_of(product_evidence: dict[str, Any] | None, key: str) -> Any:
    """ABSENT if the key is missing, MALFORMED if present-but-not-a-dict, else the dict."""
    if not product_evidence or key not in product_evidence:
        return ABSENT
    val = product_evidence.get(key)
    return val if isinstance(val, dict) else MALFORMED


def is_str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(i, str) for i in x)


def is_str_dict(x: Any) -> bool:
    return isinstance(x, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in x.items())


def is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def malformed(oracle: str, why: str, facts: dict[str, Any] | None = None) -> OracleResult:
    return failing(
        oracle,
        fc.EDIT_ORACLE_EVIDENCE_MALFORMED,
        first_broken_link="edit_evidence -> oracle",
        facts={"why": why, **(facts or {})},
    )

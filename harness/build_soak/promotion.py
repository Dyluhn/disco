"""HARN-3 — the centralized PRODUCT promotion policy — compatibility facade.

The actual check logic now lives in :mod:`._promotion_policy`.
This module re-exports the public API so existing imports are unchanged.
"""

from __future__ import annotations

from typing import Any

from ._promotion_policy import (
    PRODUCT_HARNESS_KEYS as _PRODUCT_HARNESS_KEYS,
)
from ._promotion_policy import (
    PROVIDER_VIOLATIONS as _PROVIDER_VIOLATIONS,
)
from ._promotion_policy import (
    build_checks,
    evaluate_eligible,
)

__all__ = [
    "_PRODUCT_HARNESS_KEYS",
    "_PROVIDER_VIOLATIONS",
    "evaluate_product_promotion",
]


def evaluate_product_promotion(
    classifications: list[dict[str, Any]],
    *,
    product_harness: dict[str, Any] | None = None,
    require_product_harness: bool = True,
) -> dict[str, Any]:
    """Return ``{"eligible": bool, "checks": [{"check", "ok", "detail"}]}``.

    A check with ``ok is None`` is informational (not blocking), matching the
    bakeoff gate convention. ``eligible`` is True only when no check is False.

    ``require_product_harness`` defaults to **True** as of A4 step 4 — "then and only
    then flip `require_product_harness` to `True`" — which became due when the F28
    rehearsal set came back fully green: all **ten** formerly-SKIPping oracles
    adjudicated PASS-or-FAIL at ONE sealed sha
    (`0aa79e7e2c208486eceb0760a2558353d3060752`) with **zero** browser-reason SKIPs and
    **zero** reds, across three legs whose own identity files all agree. Receipt:
    `automation/runtime/boundary-20260806i-pkg03-f28-leg-c/` (`ADJUDICATION-MAP.txt`).

    Until that measurement existed the default could not honestly be True: the check
    would have blocked promotion on evidence no governed run was able to produce. Passing
    ``require_product_harness=False`` explicitly is still supported and still
    informational — the flip changes the default, not the mechanism, and no oracle,
    threshold or sub-gate was altered to reach it.
    """
    checks = build_checks(
        classifications,
        product_harness=product_harness,
        require_product_harness=require_product_harness,
    )
    eligible = evaluate_eligible(checks)
    return {"eligible": eligible, "checks": checks}

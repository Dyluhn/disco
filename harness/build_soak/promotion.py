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
    require_product_harness: bool = False,
) -> dict[str, Any]:
    """Return ``{"eligible": bool, "checks": [{"check", "ok", "detail"}]}``.

    A check with ``ok is None`` is informational (not blocking), matching the
    bakeoff gate convention. ``eligible`` is True only when no check is False.
    """
    checks = build_checks(
        classifications,
        product_harness=product_harness,
        require_product_harness=require_product_harness,
    )
    eligible = evaluate_eligible(checks)
    return {"eligible": eligible, "checks": checks}

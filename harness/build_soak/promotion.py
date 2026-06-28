"""HARN-3 — the centralized PRODUCT promotion policy.

Distinct from bakeoff.py (which compares the Pi vs Disco KERNELS). This composes a
set of run classifications (plus, later, the browser product-harness result) into a
single promotion verdict: a build surface is promotable only when EVERY gate holds.

Gates (headless, enforceable now):
- at least one run classified
- every run is PASS (no FAIL)
- no INVALID_RUN (an adjudication gap is not a pass)
- no UNKNOWN_FAILURE (an unexplained failure blocks promotion)
- the PROVIDER constraint holds: no PROVIDER_FORBIDDEN / PROVIDER_WRONG_MODEL /
  PROVIDER_CALL_AFTER_TERMINAL (MiniMax-only / no-OpenRouter / zero-calls-after-terminal)

Gates pending HARN-1b (browser product harness):
- the product harness ran green: browser WS connected, preview visible, download
  verified, cleanup ok, zero provider calls after terminal. Enforced only when
  ``require_product_harness=True`` (default False until HARN-1b exists, so this gate
  reports as not-yet-required rather than blocking on absent browser evidence).
"""

from __future__ import annotations

from typing import Any

from . import failure_codes as fc

# Provider-ledger violation codes that must never appear in a promotable run.
_PROVIDER_VIOLATIONS = frozenset(
    {fc.PROVIDER_FORBIDDEN, fc.PROVIDER_WRONG_MODEL, fc.PROVIDER_CALL_AFTER_TERMINAL}
)

# The product-harness sub-gates HARN-1b must report all True.
_PRODUCT_HARNESS_KEYS = (
    "browser_ws_connected",
    "preview_visible",
    "download_verified",
    "cleanup_ok",
    "zero_provider_calls_after_terminal",
)


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
    checks: list[dict[str, Any]] = []

    def chk(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    runs = list(classifications or [])
    chk("runs present", len(runs) > 0, f"{len(runs)} run(s) classified")

    if runs:
        non_pass = [c for c in runs if c.get("status") != fc.PASS]
        chk("all runs PASS", not non_pass, f"{len(non_pass)} non-PASS run(s)")

        invalid = [c for c in runs if c.get("status") == fc.INVALID_RUN]
        chk("no INVALID_RUN", not invalid, f"{len(invalid)} invalid run(s)")

        unknown = [c for c in runs if c.get("code") == fc.UNKNOWN_FAILURE]
        chk("no UNKNOWN_FAILURE", not unknown, f"{len(unknown)} unknown-failure run(s)")

        provider_bad = [c for c in runs if c.get("code") in _PROVIDER_VIOLATIONS]
        chk(
            "provider constraint (MiniMax-only / no-OpenRouter / no calls after terminal)",
            not provider_bad,
            (
                "clean"
                if not provider_bad
                else f"{len(provider_bad)} provider violation(s): "
                + ", ".join(sorted({str(c.get('code')) for c in provider_bad}))
            ),
        )

    # product harness gate
    if require_product_harness:
        if not product_harness:
            chk("product harness green", False, "required but no product-harness result supplied")
        else:
            failed_keys = [k for k in _PRODUCT_HARNESS_KEYS if not product_harness.get(k)]
            chk(
                "product harness green",
                not failed_keys,
                "all sub-gates green" if not failed_keys else f"failed: {failed_keys}",
            )
    else:
        chk(
            "product harness green",
            None,
            "not required yet (HARN-1b browser harness pending)",
        )

    eligible = all(c["ok"] for c in checks if c["ok"] is not None) and not any(
        c["ok"] is False for c in checks
    )
    return {"eligible": eligible, "checks": checks}

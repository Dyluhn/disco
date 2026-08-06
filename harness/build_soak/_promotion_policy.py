"""Promotion check functions for the centralized PRODUCT promotion policy.

Each check is a pure function of the classifications list. The checks are
ordered and composed by :func:`evaluate_product_promotion` in ``promotion.py``.
"""

from __future__ import annotations

from typing import Any

from . import failure_codes as fc

# Provider-ledger violation codes that must never appear in a promotable run.
PROVIDER_VIOLATIONS = frozenset(
    {fc.PROVIDER_FORBIDDEN, fc.PROVIDER_WRONG_MODEL, fc.PROVIDER_CALL_AFTER_TERMINAL}
)

# The product-harness sub-gates HARN-1b must report all True.
PRODUCT_HARNESS_KEYS = (
    "browser_ws_connected",
    "preview_visible",
    "download_verified",
    "cleanup_ok",
    "zero_provider_calls_after_terminal",
)


def _check_runs_present(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "check": "runs present",
        "ok": len(runs) > 0,
        "detail": f"{len(runs)} run(s) classified",
    }


def _check_all_pass(runs: list[dict[str, Any]]) -> dict[str, Any]:
    non_pass = [c for c in runs if c.get("status") != fc.PASS]
    return {
        "check": "all runs PASS",
        "ok": not non_pass,
        "detail": f"{len(non_pass)} non-PASS run(s)",
    }


def _check_no_invalid(runs: list[dict[str, Any]]) -> dict[str, Any]:
    invalid = [c for c in runs if c.get("status") == fc.INVALID_RUN]
    return {
        "check": "no INVALID_RUN",
        "ok": not invalid,
        "detail": f"{len(invalid)} invalid run(s)",
    }


def _check_no_unknown(runs: list[dict[str, Any]]) -> dict[str, Any]:
    unknown = [c for c in runs if c.get("code") == fc.UNKNOWN_FAILURE]
    return {
        "check": "no UNKNOWN_FAILURE",
        "ok": not unknown,
        "detail": f"{len(unknown)} unknown-failure run(s)",
    }


def _check_provider_constraint(runs: list[dict[str, Any]]) -> dict[str, Any]:
    provider_bad = [c for c in runs if c.get("code") in PROVIDER_VIOLATIONS]
    return {
        "check": "provider constraint (MiniMax-only / no-OpenRouter / no calls after terminal)",
        "ok": not provider_bad,
        "detail": (
            "clean"
            if not provider_bad
            else f"{len(provider_bad)} provider violation(s): "
            + ", ".join(sorted({str(c.get("code")) for c in provider_bad}))
        ),
    }


def _check_product_harness(
    product_harness: dict[str, Any] | None,
    require_product_harness: bool,
) -> dict[str, Any]:
    if not require_product_harness:
        return {
            "check": "product harness green",
            "ok": None,
            # Reached only when a caller opts out EXPLICITLY: the default is True as of
            # A4 step 4. The previous wording ("not required yet — HARN-1b browser
            # harness pending") described a world that no longer exists; HARN-1b landed
            # at `a9daf96d` and the ten adjudicated green at one sealed sha.
            "detail": "not required — caller opted out explicitly (default is required)",
        }
    if not product_harness:
        return {
            "check": "product harness green",
            "ok": False,
            "detail": "required but no product-harness result supplied",
        }
    failed_keys = [k for k in PRODUCT_HARNESS_KEYS if not product_harness.get(k)]
    return {
        "check": "product harness green",
        "ok": not failed_keys,
        "detail": "all sub-gates green" if not failed_keys else f"failed: {failed_keys}",
    }


def build_checks(
    classifications: list[dict[str, Any]],
    *,
    product_harness: dict[str, Any] | None,
    require_product_harness: bool,
) -> list[dict[str, Any]]:
    """Build the ordered list of promotion checks."""
    runs = list(classifications or [])
    checks: list[dict[str, Any]] = [_check_runs_present(runs)]
    if runs:
        checks.append(_check_all_pass(runs))
        checks.append(_check_no_invalid(runs))
        checks.append(_check_no_unknown(runs))
        checks.append(_check_provider_constraint(runs))
    checks.append(_check_product_harness(product_harness, require_product_harness))
    return checks


def evaluate_eligible(checks: list[dict[str, Any]]) -> bool:
    """Eligible only when no check is False."""
    return all(c["ok"] for c in checks if c["ok"] is not None) and not any(
        c["ok"] is False for c in checks
    )

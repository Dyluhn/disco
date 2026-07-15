"""Scenario-scoped browser-verification contract (H191).

This is deliberately separate from the product-harness browser oracles.  Those
oracles are optional for a headless soak and therefore SKIP when their UI evidence
slice is absent.  A scenario can nevertheless promise browser verification in its
own machine-readable contract; that promise is adjudicable from durable verifier
observations plus H190's hash-locked screenshot bytes.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ScenarioBrowserVerificationOracle"


def _admissible_screenshot_path(path: str) -> bool:
    """Mirror the H190 evidence namespace without importing the live API adapter."""
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and "\x00" not in path
        and len(parts) == 3
        and parts[:2] == [".pmx", "screenshots"]
        and not any(part in {"", ".", ".."} for part in parts)
        and parts[2].endswith(".png")
    )


class ScenarioBrowserVerificationOracle:
    """Require a real passing verifier and its durable screenshot when opted in."""

    def check(
        self,
        *,
        scenario: dict[str, Any] | None,
        verification_paths: set[str],
        browser_evidence_paths: set[str],
    ) -> list[OracleResult]:
        assertion = ((scenario or {}).get("assertions") or {}).get("browser_verification")
        if assertion is None:
            return [skipping(_ORACLE, reason="scenario does not require browser verification")]

        # ContractOracle has already rejected malformed assertion shapes before
        # this product oracle can run.
        if not verification_paths:
            return [
                failing(
                    _ORACLE,
                    fc.VERIFICATION_GATE_BYPASSED,
                    first_broken_link="finish -> browser_verification",
                    facts={
                        "required": True,
                        "passing_verifier_observations": 0,
                    },
                )
            ]

        admissible_locked = {
            path
            for path in browser_evidence_paths
            if isinstance(path, str) and _admissible_screenshot_path(path)
        }
        missing = sorted(verification_paths - admissible_locked)
        if missing:
            return [
                failing(
                    _ORACLE,
                    fc.MISSING_REQUIRED_EVIDENCE,
                    first_broken_link=("browser_verification -> durable_screenshot_evidence"),
                    facts={
                        "verification_paths": sorted(verification_paths),
                        "locked_browser_evidence_paths": sorted(admissible_locked),
                        "missing_or_untrusted_paths": missing,
                    },
                )
            ]

        return [
            passing(
                _ORACLE,
                facts={
                    "verification_paths": sorted(verification_paths),
                    "locked_browser_evidence_paths": sorted(admissible_locked),
                },
            )
        ]

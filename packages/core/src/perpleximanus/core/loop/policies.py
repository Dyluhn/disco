"""Confirmation policies + provisional security analyzers — agent-loop-contract.md §5.

The `SecurityAnalyzer` and `ConfirmationPolicy` are formally owned by the Security
design (BoD §17), not this contract. To make the loop runnable and testable now,
this module ships concrete, minimal versions:

- The three confirmation policies named in BoD §17.2 (`NeverConfirm`,
  `AlwaysConfirm`, `ConfirmRisky`) — these are simple decision rules, contractual
  in shape.
- Two **[PROVISIONAL]** analyzers (`NullSecurityAnalyzer`, `SelfAssessedAnalyzer`)
  so the loop has something to call until the real rule-based/ensemble analyzer
  (shell-command parser, etc.) arrives with the Security contract.
"""

from __future__ import annotations

from ..events import ActionEvent, SecurityRisk
from ..security import at_or_above  # the safe threshold comparator (security §2)

# ---- confirmation policies (security-analyzer-contract.md §5) ---------------


class NeverConfirm:
    """Never gate — the Research surface default (read-only scope, §8)."""

    name = "never_confirm"

    def should_confirm(self, risk: SecurityRisk) -> bool:
        return False


class AlwaysConfirm:
    """Gate every action (maximally cautious)."""

    name = "always_confirm"

    def should_confirm(self, risk: SecurityRisk) -> bool:
        return True


class ConfirmRisky:
    """Gate when risk is at/above a threshold, and (by default) on UNKNOWN — the
    Agent surface default. Confirm-on-UNKNOWN is the safe default (§5 / principle
    4); UNKNOWN is never ranked, it is handled explicitly."""

    name = "confirm_risky"

    def __init__(
        self, threshold: SecurityRisk = SecurityRisk.HIGH, *, confirm_unknown: bool = True
    ) -> None:
        self._threshold = threshold
        self._confirm_unknown = confirm_unknown

    def should_confirm(self, risk: SecurityRisk) -> bool:
        if risk == SecurityRisk.UNKNOWN:
            return self._confirm_unknown
        return at_or_above(risk, self._threshold)


# ---- provisional analyzers (the Security contract will supersede these) ------


class NullSecurityAnalyzer:
    """[PROVISIONAL] Always returns UNKNOWN — defers entirely to the
    ConfirmationPolicy's confirm-on-UNKNOWN behavior."""

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return SecurityRisk.UNKNOWN


class SelfAssessedAnalyzer:
    """[PROVISIONAL] Trusts the agent's own self-assessed risk. The real analyzer
    (BoD §17.2) scores independently and may override; this is a stand-in until
    that contract lands."""

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return action.self_assessed_risk

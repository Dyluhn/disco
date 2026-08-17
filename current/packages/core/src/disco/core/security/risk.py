"""Risk vocabulary helpers — security-analyzer-contract.md §2.

Reuses the event contract's `SecurityRisk` (not redefined). The two helpers
below pin the *semantics* the whole subsystem relies on:

- `at_or_above` — the safe threshold comparator. `LOW < MEDIUM < HIGH` are
  ordered; `UNKNOWN` is NOT comparable (a distinct "could not determine" state,
  handled separately by the policy, never ranked).
- `max_risk` — the ensemble combiner: the MORE cautious of two risks. `HIGH`
  dominates everything; `UNKNOWN` dominates `LOW`/`MEDIUM` (uncertainty is treated
  cautiously) but NOT `HIGH` (a known-HIGH is the most cautious).
"""

from __future__ import annotations

from ..events import SecurityRisk

# Ordered comparable risks; UNKNOWN is intentionally absent (non-comparable, §2).
_ORDER: dict[SecurityRisk, int] = {
    SecurityRisk.LOW: 1,
    SecurityRisk.MEDIUM: 2,
    SecurityRisk.HIGH: 3,
}


def at_or_above(risk: SecurityRisk, threshold: SecurityRisk) -> bool:
    """[CONTRACT] True iff `risk` is a known level >= `threshold`. UNKNOWN (on
    either side) returns False here — it is handled separately by the policy,
    never ranked (§2)."""
    if risk == SecurityRisk.UNKNOWN or threshold == SecurityRisk.UNKNOWN:
        return False
    return _ORDER[risk] >= _ORDER[threshold]


def max_risk(a: SecurityRisk, b: SecurityRisk) -> SecurityRisk:
    """[CONTRACT] The MORE cautious of two risks, for ensemble combination (§4).
    HIGH dominates everything. UNKNOWN dominates LOW/MEDIUM (uncertainty is
    treated cautiously) but NOT HIGH (a known-HIGH is the most cautious)."""
    if SecurityRisk.HIGH in (a, b):
        return SecurityRisk.HIGH
    if SecurityRisk.UNKNOWN in (a, b):
        return SecurityRisk.UNKNOWN
    if SecurityRisk.MEDIUM in (a, b):
        return SecurityRisk.MEDIUM
    return SecurityRisk.LOW

"""Risk vocabulary — security-analyzer-contract.md §9.1."""

from __future__ import annotations

import pytest
from perpleximanus.core import SecurityRisk
from perpleximanus.core.security import at_or_above, max_risk

U, L, M, H = (
    SecurityRisk.UNKNOWN,
    SecurityRisk.LOW,
    SecurityRisk.MEDIUM,
    SecurityRisk.HIGH,
)


@pytest.mark.parametrize(
    "risk,threshold,expected",
    [
        (L, L, True),
        (M, L, True),
        (H, L, True),
        (L, M, False),
        (M, M, True),
        (H, M, True),
        (M, H, False),
        (H, H, True),
        # UNKNOWN is never ranked — False on either side.
        (U, L, False),
        (U, H, False),
        (H, U, False),
        (U, U, False),
    ],
)
def test_at_or_above_orders_known_levels_and_never_ranks_unknown(risk, threshold, expected):
    assert at_or_above(risk, threshold) is expected


@pytest.mark.parametrize(
    "a,b,expected",
    [
        # HIGH dominates everything (including UNKNOWN — a known-HIGH is most cautious).
        (H, L, H),
        (L, H, H),
        (H, U, H),
        (U, H, H),
        (H, M, H),
        # UNKNOWN dominates LOW/MEDIUM (uncertainty treated cautiously).
        (U, L, U),
        (M, U, U),
        (U, U, U),
        # MEDIUM over LOW.
        (M, L, M),
        (L, M, M),
        # LOW only when both low.
        (L, L, L),
    ],
)
def test_max_risk_is_the_more_cautious(a, b, expected):
    assert max_risk(a, b) == expected

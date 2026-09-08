"""Fixture line movement keeps its original test-inventory owner."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "development" / "scripts"))

from architecture.test_inventory_parts import _splits  # noqa: E402

from architecture import inventory_static, test_inventory  # noqa: E402


def test_fixture_line_drift_preserves_historical_owner_without_new_addition():
    baseline = test_inventory.load_test_inventory(REPO_ROOT)
    drifted = copy.deepcopy(baseline)
    previous = baseline["mapping_static"]["fixtures"]
    current = drifted["mapping_static"]["fixtures"]
    # The fixture must belong to a package whose claims are validated against the
    # CURRENT inventory. A package already present in the sealed baseline
    # inventory is validated against THAT historical authority instead
    # (inventory_static.check_inventory_metadata picks `historical` for those),
    # so renaming one of its fixtures is correctly not reported as absent.
    # PKG-35-DEEP-RESEARCH-CLOSEOUT postdates the sealed baseline, so its claims
    # are checked against the live inventory, which is the case this guards.
    live = next(
        row
        for row in current
        if row["path"] == "packages/core/tests/conftest.py"
        and row["fixture"] == "_no_router_retry_backoff"
    )
    live["line"] -= 2

    assert _splits.unaccounted_fixture_line_drift([live], previous, current) == []
    assert inventory_static.check_inventory_metadata(drifted, REPO_ROOT) == []

    live["path"] = "packages/core/tests/renamed_conftest.py"
    problems = inventory_static.check_inventory_metadata(drifted, REPO_ROOT)
    assert any(
        "fixtures" in problem
        and "absent" in problem
        and "current inventory" in problem
        for problem in problems
    )

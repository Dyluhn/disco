#!/usr/bin/env python3
"""Inventory execution-coverage check — stable wrapper.

Fails when ``development/architecture/test-inventory.json`` certifies a test id that no
sanctioned command (a Makefile hermetic target or a CI workflow step) is able to
collect. PKG-19-CERT-STRUCTURAL found 87 such ids across two findings, plus a
whole certified root no command names; none of it was visible to any gate,
because no gate compared the two sets.

Usage:
    uv run python development/scripts/check_inventory_execution.py
    uv run python development/scripts/check_inventory_execution.py --root /tmp/test-repo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.inventory_execution import check_inventory_execution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    args = parser.parse_args()

    result = check_inventory_execution(root=args.root)

    if not result["ok"]:
        print("INVENTORY EXECUTION FAIL — certified ids no sanctioned command runs:\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        print(
            f"\n  certified {result['certified_total']}; "
            f"reachable {result['executable_total']}; "
            f"orphaned {result['orphan_total']}"
        )
        return 1

    print(
        f"INVENTORY EXECUTION OK — all {result['certified_total']} certified IDs are "
        f"reachable by a sanctioned command ({result['executable_total']} collected)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

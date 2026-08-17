#!/usr/bin/env python3
"""Test inventory check — stable wrapper.

Verifies that the collected test inventory matches the sealed baseline with no
unexplained deletion, deselection, or marker drift.

Always runs all collectors: four pytest roots, the exact five deselected node
IDs, Vitest, and every Playwright config. There is no collector-free mode.
The checker inherits the process environment and uses
``sys.executable -m pytest``.

Usage:
    uv run python development/scripts/check_test_inventory.py
    uv run python development/scripts/check_test_inventory.py --root /tmp/test-repo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.test_inventory import check_test_inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    args = parser.parse_args()

    result = check_test_inventory(root=args.root)

    if not result["ok"]:
        print("TEST INVENTORY FAIL — inventory drift:\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    print(
        f"TEST INVENTORY OK — {result['collected_total']} collected IDs; "
        f"{result['python_static_ids']} Python static IDs; "
        f"{result['typescript_static_ids']} TypeScript static IDs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

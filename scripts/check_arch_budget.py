#!/usr/bin/env python3
"""Architecture size budget — stable wrapper (run in CI / pre-commit).

This is the stable entrypoint. It parses arguments, calls the bounded helper in
``scripts/architecture/budget.py``, and renders a deterministic result.

Usage:
    uv run python scripts/check_arch_budget.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.budget import run_budget_check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing on success",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    args = parser.parse_args()

    result = run_budget_check(scan_typescript=True, root=args.root)

    if not result["ok"]:
        print("ARCH BUDGET FAIL — budget/debt violation(s):\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    if not args.quiet:
        stats = result["stats"]
        print(
            f"ARCH BUDGET OK — {stats['python_modules']} Python modules, "
            f"{stats['typescript_modules']} TypeScript modules scanned; "
            f"{stats['debt_count']} active debt rows; "
            f"{stats['current_violations']} current violations match sealed debt."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
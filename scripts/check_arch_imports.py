#!/usr/bin/env python3
"""Architecture import/context graph check — stable wrapper.

Enforces package/internal/frontend context edges, cycles, static and literal
dynamic imports, using one registry shared with the generated diagram.

Usage:
    uv run python scripts/check_arch_imports.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.imports import check_imports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    args = parser.parse_args()

    result = check_imports(args.root)

    if not result["ok"]:
        print("ARCH IMPORTS FAIL — import/context violation(s):\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    if not args.quiet:
        print(
            f"ARCH IMPORTS OK — {result['edge_count']} import edges; "
            f"{result['upward_edge_count']} upward edges "
            f"({len(result['exemption_edges_found'])} DM-005 exemptions); "
            f"{result['cycle_count']} cycles."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

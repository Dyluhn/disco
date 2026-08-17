#!/usr/bin/env python3
"""CI/release/pre-commit contract check — stable wrapper.

Verifies that CI, release, and pre-commit structurally require the exact
ordered architecture gates, with Node/npm provisioning preceding the
TypeScript scanner and tool_schemas.py retained after the CI-order self-check.

Usage:
    uv run python development/scripts/check_ci_contract.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.ci_contract import check_ci_contract


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

    result = check_ci_contract(args.root)

    if not result["ok"]:
        print("CI CONTRACT FAIL — CI/release/pre-commit order violation(s):\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    if not args.quiet:
        print(
            "CI CONTRACT OK — CI, release, and pre-commit all enforce the "
            "exact ordered architecture gates."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
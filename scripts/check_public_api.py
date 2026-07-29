#!/usr/bin/env python3
"""Public API baseline check — stable wrapper.

Verifies that current public API surfaces match the sealed baseline and that
no public surface deletion occurs without a compatible bridge.

Usage:
    uv run python scripts/check_public_api.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.public_api import check_public_api


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

    result = check_public_api(args.root)

    if not result["ok"]:
        print("PUBLIC API FAIL — baseline drift or deletion:\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    if not args.quiet:
        print(
            f"PUBLIC API OK — {result['initializer_count']} initializers; "
            f"{result['frontend_module_count']} frontend modules; "
            f"{result['contract_file_count']} contract files verified."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Architecture debt schema and ratchet check — stable wrapper.

Verifies the sealed debt ledger schema, the immutable 991-ID universe, the
965-row active count, and the three resolved PKG-02-GATE dispositions.

Usage:
    uv run python scripts/check_arch_debt.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.debt import run_debt_check

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATE_DEBT = Path(__file__).resolve().parent / "architecture" / "generate_debt.py"


def _check_generated_authorities(root: Path) -> list[str]:
    """Run the deterministic authority byte comparison for ``root``."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATE_DEBT),
                "--check",
                "--repo-root",
                str(root),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        return [f"generate_debt.py --check timed out after {exc.timeout} seconds"]
    except OSError as exc:
        return [f"generate_debt.py --check could not execute: {exc}"]

    if result.returncode == 0:
        return []
    detail = "\n".join(
        output.strip()
        for output in (result.stdout, result.stderr)
        if output.strip()
    )
    if not detail:
        detail = "no diagnostics"
    return [
        f"generate_debt.py --check exited {result.returncode}:\n{detail}"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (defaults to this script's repository)",
    )
    args = parser.parse_args()

    root = (args.root or REPO_ROOT).resolve()
    generation_problems = _check_generated_authorities(root)
    if generation_problems:
        print("ARCH DEBT FAIL — generated authority verification failed:\n")
        for problem in generation_problems:
            print(f"  - {problem}")
        return 1

    result = run_debt_check(current_violations=None, root=root)

    if not result["ok"]:
        print("ARCH DEBT FAIL — debt schema/ratchet violation(s):\n")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 1

    if not args.quiet:
        print(
            f"ARCH DEBT OK — {result['debt_count']} active debt rows; "
            "991 immutable dispositions; 3 resolved PKG-02-GATE IDs."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

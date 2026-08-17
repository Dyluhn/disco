#!/usr/bin/env python3
"""Deterministic seal gate for the change-controlled governance and gate files.

This is the stable entrypoint. It delegates to the bounded helper in
``development/scripts/architecture/seal.py`` which verifies both byte integrity and the
structural invocation contract (SEAL-INVOCATION.json).

The protected set includes:
  - the two sealed standards (ENGINEERING-STANDARDS.md, ARCHITECTURE-BOUNDARIES.md)
  - the architecture policy (development/architecture/policy.json)
  - the immutable disposition-ID list (development/architecture/disposition-ids.txt)
  - the stable budget checker (development/scripts/check_arch_budget.py)
  - the governance checker (development/scripts/check_governance_seal.py)
  - the SEAL-INVOCATION contract (current/docs/governance/SEAL-INVOCATION.json)

The digest manifest (PROTECTED.sha256) does NOT protect itself.

Exit codes
----------
0   every protected file matches the manifest and invocation structure is valid
1   drift: a digest mismatch, a missing file, a manifest/protected-set
    disagreement, or an invocation-structure violation
3   the manifest is absent or malformed (the seal is not established)

Rebaseline (owner-only)
-----------------------
    DISCO_GOVERNANCE_REBASELINE=1 \\
        uv run python development/scripts/check_governance_seal.py --rebaseline
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from architecture.policy import REPO_ROOT
from architecture.seal import (
    MANIFEST,
    PROTECTED,
    check_seal,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help="rewrite the manifest; requires DISCO_GOVERNANCE_REBASELINE=1",
    )
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

    root = args.root if args.root is not None else REPO_ROOT

    if args.rebaseline:
        if os.environ.get("DISCO_GOVERNANCE_REBASELINE") != "1":
            print(
                "REFUSED: --rebaseline requires DISCO_GOVERNANCE_REBASELINE=1 "
                "and an explicit owner instruction.",
                file=sys.stderr,
            )
            return 1
        invalid = [
            rel
            for rel in PROTECTED
            if not (root / rel).is_file() or (root / rel).is_symlink()
        ]
        if invalid:
            for rel in invalid:
                print(f"MISSING OR NON-REGULAR: {rel}", file=sys.stderr)
            return 1
        sealed = write_manifest(root)
        print(f"Rebaselined {MANIFEST} over {len(sealed)} protected file(s).")
        for rel in sealed:
            from architecture.seal import digest
            print(f"  {digest(root / rel)}  {rel}")
        return 0

    result = check_seal(root)

    if not result["ok"]:
        if result.get("code") == 3:
            print(
                f"SEAL NOT ESTABLISHED: {MANIFEST} is absent or malformed.",
                file=sys.stderr,
            )
            return 3
        print("Governance seal FAILED:\n", file=sys.stderr)
        for problem in result["problems"]:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nThese files are change-controlled. If this change was NOT "
            "authorised by the owner, revert it. If it WAS authorised, "
            "follow the rebaseline procedure.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(
            f"Governance seal OK ({result['protected_count']} protected files "
            "verified; invocation structure valid)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

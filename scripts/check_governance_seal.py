#!/usr/bin/env python3
"""Deterministic seal gate for the change-controlled governance files.

Two governance files state standards that an agent must not silently drift:

    docs/governance/ENGINEERING-STANDARDS.md
    docs/governance/ARCHITECTURE-BOUNDARIES.md

This script is the **authoritative backstop**.  The Claude Code PreToolUse guard
is a fast, friendly first line of defence, but a guard can always be evaded by
some shell spelling nobody enumerated.  A hash gate cannot: if the bytes moved,
this exits non-zero, and that is what CI and the session-start check consult.

The protected set lives here in code, not only in the manifest, so that dropping
an entry *from the manifest* is itself detected as drift.

Exit codes
----------
0   every protected file matches the manifest
1   drift: a digest mismatch, a missing file, or a manifest/protected-set
    disagreement
3   the manifest is absent or malformed (the seal is not established)

Rebaseline (owner-only)
-----------------------
    DISCO_GOVERNANCE_REBASELINE=1 .venv/bin/python3 \\
        scripts/check_governance_seal.py --rebaseline

Setting that variable without an explicit owner instruction is a governance
violation, not a shortcut.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

PROTECTED: tuple[str, ...] = (
    "docs/governance/ENGINEERING-STANDARDS.md",
    "docs/governance/ARCHITECTURE-BOUNDARIES.md",
)

MANIFEST = "docs/governance/PROTECTED.sha256"

REBASELINE_ENV = "DISCO_GOVERNANCE_REBASELINE"

_HEADER = """\
# Seal manifest for the change-controlled governance files.
#
# Verified by scripts/check_governance_seal.py, which is the authoritative
# backstop for docs/governance/ENGINEERING-STANDARDS.md and
# docs/governance/ARCHITECTURE-BOUNDARIES.md.
#
# Do not hand-edit.  Rebaselining requires an explicit owner instruction and the
# procedure in docs/governance/README.md.
"""


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(path: Path) -> dict[str, str] | None:
    """Parse `<sha256>  <repo-relative-path>` lines; None if unusable."""
    if not path.is_file():
        return None
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            return None
        value, rel = parts[0], parts[1].strip()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return None
        entries[rel] = value
    return entries


def write_manifest(path: Path, root: Path) -> None:
    lines = [_HEADER]
    for rel in PROTECTED:
        lines.append(f"{digest(root / rel)}  {rel}\n")
    path.write_text("".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help=f"rewrite the manifest; requires {REBASELINE_ENV}=1",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing on success",
    )
    args = parser.parse_args()

    root = repo_root()
    manifest_path = root / MANIFEST

    if args.rebaseline:
        if os.environ.get(REBASELINE_ENV) != "1":
            print(
                f"REFUSED: --rebaseline requires {REBASELINE_ENV}=1 and an explicit\n"
                f"         owner instruction.  See docs/governance/README.md.",
                file=sys.stderr,
            )
            return 1
        missing = [rel for rel in PROTECTED if not (root / rel).is_file()]
        if missing:
            for rel in missing:
                print(f"MISSING: {rel}", file=sys.stderr)
            return 1
        write_manifest(manifest_path, root)
        print(f"Rebaselined {MANIFEST} over {len(PROTECTED)} protected file(s).")
        for rel in PROTECTED:
            print(f"  {digest(root / rel)}  {rel}")
        return 0

    recorded = read_manifest(manifest_path)
    if recorded is None:
        print(
            f"SEAL NOT ESTABLISHED: {MANIFEST} is absent or malformed.\n"
            f"The governance standards are not currently change-controlled.",
            file=sys.stderr,
        )
        return 3

    problems: list[str] = []

    for rel in PROTECTED:
        full = root / rel
        if not full.is_file():
            problems.append(f"MISSING FILE: {rel} is protected but does not exist")
            continue
        if rel not in recorded:
            problems.append(f"UNSEALED: {rel} is protected but absent from {MANIFEST}")
            continue
        actual = digest(full)
        if actual != recorded[rel]:
            problems.append(
                f"DRIFT: {rel}\n"
                f"         recorded sha256:{recorded[rel]}\n"
                f"         actual   sha256:{actual}"
            )

    for rel in recorded:
        if rel not in PROTECTED:
            problems.append(
                f"STALE ENTRY: {MANIFEST} seals {rel}, which is not in the protected set"
            )

    if problems:
        print("Governance seal FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nThese files are change-controlled.  If this change was NOT authorised\n"
            "by the owner, revert it:  git checkout -- " + " ".join(PROTECTED) + "\n"
            "If it WAS authorised, follow the rebaseline procedure in\n"
            "docs/governance/README.md.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"Governance seal OK ({len(PROTECTED)} protected files verified).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Deterministic fingerprints over the working tree, without mutating it.

Two distinct digests, because they answer two different questions:

``source``
    Covers only what can change product or harness *behaviour*: the Python
    packages, the frontend, the harness, the scripts, and the build/lint
    configuration.  This is the digest that governs counted soak credit --
    a change here resets it.  Writing a governance status file does not.

``tree``
    Covers every tracked, non-ignored file in the working tree.  This is what
    candidate *freeze* cares about: after freeze the repository bytes must not
    move at all.

Both digests are computed by reading worktree file contents directly.  Nothing
is staged, stashed, or written -- measuring the tree must never mutate it.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

# Prefixes whose contents can change product or harness behaviour.
SOURCE_PREFIXES: tuple[str, ...] = (
    "current/packages/",
    "current/frontend/",
    "development/harness/",
    "development/scripts/",
)

# Individual build/lint/type configuration files outside those prefixes.
SOURCE_FILES: frozenset[str] = frozenset(
    {
        ".importlinter",
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
    }
)

# Never behaviour-bearing, even under a source prefix.
EXCLUDED_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo")
EXCLUDED_PARTS: frozenset[str] = frozenset({"__pycache__", "node_modules", ".venv"})


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def tracked_and_untracked(root: Path) -> list[str]:
    """Every tracked or untracked-but-not-ignored path, as git sees them."""
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
        cwd=root,
    )
    return sorted(p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p)


def is_source(path: str) -> bool:
    if any(part in EXCLUDED_PARTS for part in Path(path).parts):
        return False
    if path.endswith(EXCLUDED_SUFFIXES):
        return False
    if path in SOURCE_FILES:
        return True
    return path.startswith(SOURCE_PREFIXES)


def digest_paths(root: Path, paths: list[str]) -> tuple[str, int]:
    """Digest (path, content-hash) pairs in sorted path order."""
    outer = hashlib.sha256()
    counted = 0
    for rel in paths:
        full = root / rel
        try:
            data = full.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            # A submodule dir or a path deleted between listing and reading.
            continue
        inner = hashlib.sha256(data).hexdigest()
        outer.update(rel.encode("utf-8", "surrogateescape"))
        outer.update(b"\0")
        outer.update(inner.encode("ascii"))
        outer.update(b"\n")
        counted += 1
    return outer.hexdigest(), counted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--which",
        choices=("source", "tree", "both"),
        default="both",
        help="which fingerprint to emit (default: both)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="emit only the bare digest(s), one per line",
    )
    args = parser.parse_args()

    root = repo_root()
    every = tracked_and_untracked(root)

    if args.which in ("source", "both"):
        source_paths = [p for p in every if is_source(p)]
        digest, count = digest_paths(root, source_paths)
        print(digest if args.quiet else f"source  sha256:{digest}  ({count} files)")

    if args.which in ("tree", "both"):
        tree_paths = [
            p
            for p in every
            if not any(part in EXCLUDED_PARTS for part in Path(p).parts)
            and not p.endswith(EXCLUDED_SUFFIXES)
        ]
        digest, count = digest_paths(root, tree_paths)
        print(digest if args.quiet else f"tree    sha256:{digest}  ({count} files)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

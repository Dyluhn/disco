#!/usr/bin/env python3
"""Refuse to commit while a counted or qualifying build-soak batch is running.

Every counted trial stamps `repo_revision` / `repo_dirty` at ITS OWN start, so a
commit landing mid-lane gives later trials a different recorded revision than
earlier ones. The closeout then has to argue the difference was immaterial
instead of simply showing one SHA across all 100.

This has now happened twice:

  * Epic 4 — a ledger commit landed 25 seconds into the seed-460001 diagnostic.
    Harmless there (diagnostics are not counted).
  * Epic 6 — a source edit landed while the mixed pilot was still running.
    Also harmless, but only because the revision is stamped once at batch start
    and the pilot was not counted. Relying on that ordering is luck, not rigour.

So the rule stops being a habit and becomes a gate. It fails CLOSED on the thing
it is protecting and stays silent otherwise; it is a guard, not a linter.

Override for a genuine emergency:

    DISCO_ALLOW_COMMIT_DURING_SOAK=1 git commit ...

The override is deliberately explicit and deliberately logged — an operator who
needs it can have it, but not by accident.
"""

from __future__ import annotations

import os
import subprocess
import sys

# The runner module every counted and qualifying lane executes. Matching the
# module path rather than a script name keeps this true for `python -m` launches,
# the profile lane, and any wrapper around them.
_SOAK_MARKERS = (
    "harness.build_soak.run",
    "harness.build_soak.profile",
)

_OVERRIDE = "DISCO_ALLOW_COMMIT_DURING_SOAK"


def running_soak_commands() -> list[str]:
    """Command lines of any live soak process, newest first. Empty when clear."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # A guard that cannot observe the host must not block the developer; the
        # campaign's own lane scripts refuse to START on a dirty tree, so this is
        # defence in depth rather than the only control.
        return []
    mine = str(os.getpid())
    found: list[str] = []
    for line in out.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid == mine or not args:
            continue
        if any(marker in args for marker in _SOAK_MARKERS):
            found.append(args.strip()[:160])
    return found


def main() -> int:
    if os.environ.get(_OVERRIDE) == "1":
        print(f"[soak-freeze] {_OVERRIDE}=1 — commit allowed during an active soak.")
        return 0
    running = running_soak_commands()
    if not running:
        return 0
    print("REFUSED: a build-soak batch is running; the repository is frozen.", file=sys.stderr)
    print("", file=sys.stderr)
    for command in running[:4]:
        print(f"  {command}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Every counted trial stamps repo_revision at its own start, so a commit now\n"
        "would split one campaign across two SHAs. Wait for the lane to finish and\n"
        "its evidence to seal, then commit.\n"
        f"\nGenuine emergency: {_OVERRIDE}=1 git commit ...",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

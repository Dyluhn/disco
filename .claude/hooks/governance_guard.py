#!/usr/bin/env python3
"""PreToolUse guard: seal enforcement + "review before the next source mutation".

Two independent refusals:

1. **Sealed files.**  `Edit`/`Write`/`NotebookEdit` targeting a protected
   governance file is denied, as are obvious `Bash` mutations of one.

2. **Overdue review.**  While an hourly review is overdue, source mutations are
   denied so the review happens *before the next source mutation*, as required.
   The governance ledgers stay writable -- otherwise the agent could not perform
   the very review that clears the block.

This guard is a fast first line of defence, not a security boundary.  A shell
can always spell a write in some way nobody enumerated, and chasing that is
explicitly out of scope.  `development/scripts/check_governance_seal.py` is the
authoritative backstop: if bytes moved, the gate fails regardless of how.

`DISCO_GOVERNANCE_REBASELINE=1` lifts refusal (1) for a deliberate owner-driven
rebaseline.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    LEDGER_PATHS,
    emit,
    humanize,
    load_state,
    project_dir,
    protected_paths,
    read_hook_input,
    review_is_due,
    seconds_until_due,
)

FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Source prefixes whose mutation should wait for an overdue review.
SOURCE_PREFIXES = ("current/packages/", "current/frontend/", "development/harness/", "development/scripts/")
SOURCE_CONFIG = {".importlinter", "pyproject.toml", "uv.lock", "package.json"}

# Best-effort shell mutation signals.  Deliberately simple; see the docstring.
#
# Every mutator pattern must span to the end of its shell segment, so the target
# path is inside the matched text being tested.  A pattern that stops early
# (e.g. `\bsed\b[^|;]*-i`) matches only "sed -i" and the path check then never
# sees the filename -- a silent fail-open.
_REDIRECT = re.compile(r"(?<![0-9<>])>>?\s*(?P<target>[^\s;|&]+)")
_MUTATORS = (
    re.compile(r"\bsed\b[^|;&]*?-i[^|;&]*"),
    re.compile(r"\bperl\b[^|;&]*?-[a-zA-Z]*i[^|;&]*"),
    re.compile(r"\btee\b[^|;&]*"),
    re.compile(r"\btruncate\b[^|;&]*"),
    re.compile(r"\b(?:rm|mv|cp|install|dd|shred|ln)\b[^|;&]*"),
    re.compile(r"\bgit\s+(?:checkout|restore|clean|apply)\b[^|;&]*"),
    re.compile(r"\bpatch\b[^|;&]*"),
    re.compile(r"\b(?:python3?|node|ruby)\b[^|;&]*?-[ce][^|;&]*"),
)


def deny(reason: str) -> None:
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
    sys.exit(0)


def allow_silently() -> None:
    # An empty object leaves the normal permission flow untouched.
    emit({})
    sys.exit(0)


def relative(root: Path, raw: str) -> str | None:
    if not raw:
        return None
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        return str(candidate.resolve().relative_to(root))
    except (ValueError, OSError):
        return None


def is_source_path(rel: str) -> bool:
    if rel in LEDGER_PATHS:
        return False
    if rel in SOURCE_CONFIG:
        return True
    return rel.startswith(SOURCE_PREFIXES)


def bash_touches(command: str, protected: tuple[str, ...]) -> str | None:
    """Return the protected path a command plausibly mutates, else None."""
    names = {p: (p, Path(p).name) for p in protected}
    for rel, (full, base) in names.items():
        if full not in command and base not in command:
            continue
        for match in _REDIRECT.finditer(command):
            target = match.group("target")
            if base in target or full in target:
                return rel
        for pattern in _MUTATORS:
            for match in pattern.finditer(command):
                if full in match.group(0) or base in match.group(0):
                    return rel
    return None


def main() -> int:
    payload = read_hook_input()
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        allow_silently()

    root = project_dir()
    protected = protected_paths(root)
    rebaseline = os.environ.get("DISCO_GOVERNANCE_REBASELINE") == "1"

    # ---- refusal 1: sealed files -----------------------------------------
    if not rebaseline:
        if tool in FILE_TOOLS:
            rel = relative(root, str(tool_input.get("file_path") or ""))
            if rel and rel in protected:
                deny(
                    f"BLOCKED: {rel} is a change-controlled governance file.\n\n"
                    "It states standards an agent may not silently drift. Editing it "
                    "requires an explicit instruction from the owner and the "
                    "rebaseline procedure in current/docs/governance/README.md.\n\n"
                    "If you believe the standard itself is wrong, record the "
                    "disagreement in current/docs/governance/CAMPAIGN-STATUS.md and continue "
                    "the campaign under the existing standard."
                )

        if tool == "Bash":
            command = str(tool_input.get("command") or "")
            hit = bash_touches(command, protected)
            if hit:
                deny(
                    f"BLOCKED: this command appears to modify {hit}, a "
                    "change-controlled governance file.\n\n"
                    "Rebaselining requires an explicit owner instruction and the "
                    "procedure in current/docs/governance/README.md. "
                    "development/scripts/check_governance_seal.py verifies the seal regardless of "
                    "how a write is spelled."
                )

    # ---- refusal 2: overdue review before the next source mutation --------
    if tool in FILE_TOOLS:
        rel = relative(root, str(tool_input.get("file_path") or ""))
        if rel and is_source_path(rel):
            state = load_state(root)
            if review_is_due(state):
                overdue = humanize(seconds_until_due(state))
                deny(
                    f"BLOCKED: the hourly self-review is overdue by {overdue}, and "
                    f"{rel} is a source path.\n\n"
                    "The review must happen before the next source mutation. Do it "
                    "now:\n"
                    "  1. Append a complete review to current/docs/governance/SELF-REVIEWS.md "
                    "(every required field answered -- a timestamp is not a review).\n"
                    "  2. Update current/docs/governance/CAMPAIGN-STATUS.md.\n"
                    "  3. Record any evidenced pattern in "
                    "current/docs/governance/RELIABILITY-PATTERNS.md.\n\n"
                    "Completing the review advances the schedule automatically and "
                    "unblocks source edits. Then CONTINUE the campaign -- a review is "
                    "not a checkpoint and not permission to stop."
                )

    allow_silently()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never break the session on a guard bug
        print(f"governance_guard: non-fatal error: {exc}", file=sys.stderr)
        sys.exit(0)

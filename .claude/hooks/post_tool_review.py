#!/usr/bin/env python3
"""PostToolUse hook: inject the hourly review when due; acknowledge completion.

Running after the tool returns is what makes this safe: a long test run or model
call is never interrupted mid-flight.  The instruction lands immediately after
that tool returns, and `governance_guard.py` then holds the line by refusing the
next *source* mutation until the review is actually written.

Completion is detected structurally, not by a timestamp.  When SELF-REVIEWS.md
is written, the last review block is validated against every required field; a
valid block with a fresh standing ledger advances the schedule atomically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    CAMPAIGN_STATUS,
    RELIABILITY_PATTERNS,
    SELF_REVIEWS,
    advance_review,
    emit,
    humanize,
    last_review_block,
    load_state,
    missing_review_fields,
    project_dir,
    read_hook_input,
    review_is_due,
    save_state,
    seconds_until_due,
)

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# While overdue, re-inject at most this often so the instruction is present
# without flooding every single tool result.
REINJECT_SECONDS = 90

REVIEW_TEMPLATE = """\
## Review <N> — <YYYY-MM-DD HH:MM local> / <YYYY-MM-DDTHH:MM:SSZ UTC>

Source fingerprint: sha256:<.venv/bin/python3 development/scripts/source_fingerprint.py --which source --quiet>
Work completed since prior review: <what actually changed>
Evidence that it actually worked: <exit codes, test counts, evidence paths>
What went well and why: <...>
What went rough / consumed time or tokens: <...>
Immediate process or technical correction: <what you are changing now>
Recent fixes reviewed together: <the fixes considered as a group>
Repeated pattern detected? (yes/no): <yes|no>
<if yes, also answer:>
  - shared earliest broken invariant: <...>
  - structural product/harness remedy: <...>
  - signal that would recognize it earlier next time: <...>
  - existing/new regression that protects it: <...>
  - why the remedy remains target-neutral and flexible: <...>
Overhardening check:
  - observed failure or authoritative contract requiring each open item: <...>
  - any theoretical tail to drop: <...>
Next action: <the next campaign action you are taking immediately>
"""


def inject(text: str, nudge: str | None = None) -> None:
    """Emit one PostToolUse message, carrying any pending watchdog nudge with it.

    The nudge is appended rather than emitted separately: an early return for the
    nudge previously swallowed the review-acceptance path, so a review that was
    genuinely written never advanced the schedule.
    """
    if nudge:
        text = f"{text}\n\n⏱ {nudge}"
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
    )
    sys.exit(0)


def quiet() -> None:
    emit({})
    sys.exit(0)


def handle_review_written(root: Path, state: dict, nudge: str | None = None) -> None:
    block = last_review_block(root)
    missing = missing_review_fields(block)

    if missing:
        bullet = "\n".join(f"  - {field}" for field in missing)
        inject(
            "HOURLY REVIEW NOT YET ACCEPTED — the last block in "
            f"{SELF_REVIEWS} is missing required content:\n\n{bullet}\n\n"
            "A timestamp is not a review. Answer every field, then the schedule "
            "advances automatically.",
            nudge,
        )

    if not (root / CAMPAIGN_STATUS).is_file():
        inject(
            f"HOURLY REVIEW NOT YET ACCEPTED — {CAMPAIGN_STATUS} does not exist. "
            "The review must also update the standing status ledger."
        )

    state = advance_review(state, root)
    count = state.get("reviews_completed", 0)
    interval = humanize(int(state.get("interval_seconds", 3600)))
    inject(
        f"HOURLY REVIEW ACCEPTED (#{count}). Next review due in {interval}.\n\n"
        "Source mutations are unblocked. A review is not a checkpoint and not "
        "permission to stop — CONTINUE the campaign with the next action you just "
        "recorded.",
        nudge,
    )


def _pending_watchdog_nudge(root: Path) -> str | None:
    """An unacknowledged out-of-session watchdog nudge, if one is waiting.

    The watchdog runs detached and cannot inject context itself. Surfacing its
    nudge here is what actually reaches the model.
    """
    import json as _json

    path = root / ".claude" / "runtime" / "watchdog-nudge.json"
    if not path.is_file():
        return None
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("acknowledged"):
        return None
    payload["acknowledged"] = True
    try:
        path.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
    return str(payload.get("message") or "")


def main() -> int:
    payload = read_hook_input()
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    root = project_dir()
    state = load_state(root)

    # Read the nudge now, but do NOT emit it here. Emitting immediately made the
    # watchdog swallow the review-acceptance path below: a pending nudge returned
    # first, so a review that WAS written never advanced the schedule. Two
    # mechanisms both wanting to speak must not let one silently eat the other.
    nudge = _pending_watchdog_nudge(root)

    # --- did the agent just write the review? --------------------------------
    if tool in WRITE_TOOLS and isinstance(tool_input, dict):
        raw = str(tool_input.get("file_path") or "")
        if raw:
            try:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = root / candidate
                rel = str(candidate.resolve().relative_to(root))
            except (ValueError, OSError):
                rel = ""
            if rel == SELF_REVIEWS:
                handle_review_written(root, state, nudge)

    # --- otherwise: is a review due? -----------------------------------------
    if not review_is_due(state):
        if nudge:
            inject(f"⏱ {nudge}")
        quiet()

    now_overdue = humanize(seconds_until_due(state))
    last_injected = state.get("last_injection_epoch")
    import time as _time

    now = int(_time.time())
    if last_injected is not None and now - int(last_injected) < REINJECT_SECONDS:
        quiet()

    state["last_injection_epoch"] = now
    save_state(state, root)

    inject(
        f"⏰ HOURLY SELF-REVIEW IS DUE (overdue by {now_overdue}).\n\n"
        "Do this now, before your next source mutation — source edits are blocked "
        "until it is done:\n\n"
        f"1. Append a complete review to {SELF_REVIEWS} using this shape "
        "(every field answered):\n\n"
        f"{REVIEW_TEMPLATE}\n"
        f"2. Update the standing ledger {CAMPAIGN_STATUS}.\n"
        f"3. If a pattern is evidenced across at least two fixes/failures or one "
        f"demonstrated cross-cutting mechanism, record it in {RELIABILITY_PATTERNS}.\n\n"
        "Then CONTINUE immediately with the next campaign action. An hourly review "
        "is not a checkpoint, not a status report to hand back, and not permission "
        "to stop.",
        nudge,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never break the session on a hook bug
        print(f"post_tool_review: non-fatal error: {exc}", file=sys.stderr)
        sys.exit(0)

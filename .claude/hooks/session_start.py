#!/usr/bin/env python3
"""SessionStart hook: governance seal integrity + hourly-review state.

Reports, at the top of every session:

- whether the change-controlled governance files still match their seal;
- whether an hourly review is due or when the next one falls;
- where authority actually lives, so a fresh session does not reconstruct
  policy from old prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    CAMPAIGN_STATUS,
    SELF_REVIEWS,
    completion_contract_satisfied,
    emit,
    humanize,
    load_state,
    project_dir,
    read_hook_input,
    review_is_due,
    seconds_until_due,
    verify_seal,
)


def main() -> int:
    read_hook_input()  # drain stdin; contents are not needed here
    root = project_dir()
    lines: list[str] = ["=== Disco governance ==="]

    code, output = verify_seal(root)
    if code == 0:
        lines.append("Seal: OK — change-controlled standards match their manifest.")
    elif code == 3:
        lines.append(
            "Seal: NOT ESTABLISHED — the governance standards are not currently "
            "change-controlled.\n"
            f"  {output}"
        )
    else:
        lines.append(
            "Seal: FAILED — a change-controlled governance file has drifted.\n"
            f"{output}\n"
            "  Do not continue campaign work until this is reconciled. If the change "
            "was not authorised by the owner, revert it."
        )

    state = load_state(root)
    remaining = seconds_until_due(state)
    completed = state.get("reviews_completed", 0)
    interval = humanize(int(state.get("interval_seconds", 3600)))
    if review_is_due(state):
        lines.append(
            f"Hourly review: DUE NOW (overdue by {humanize(remaining)}; "
            f"{completed} completed; interval {interval}).\n"
            f"  Append a complete review to {SELF_REVIEWS} and update "
            f"{CAMPAIGN_STATUS} before your next source mutation."
        )
    else:
        lines.append(
            f"Hourly review: next due in {humanize(remaining)} "
            f"({completed} completed; interval {interval})."
        )

    if completion_contract_satisfied(root):
        lines.append("Completion contract: CAMPAIGN-STATUS.md declares Epics 0-7 satisfied.")
    else:
        lines.append(
            "Completion contract: NOT satisfied. A voluntary stop is refused until "
            f"{CAMPAIGN_STATUS} truthfully declares every Epic 0-7 acceptance item met."
        )

    lines.append(
        "Authority order: current code+tests+evidence > ENGINEERING-STANDARDS > "
        "ARCHITECTURE-BOUNDARIES > CAMPAIGN-PLAN > CURRENT-STATE/CAMPAIGN-STATUS. "
        "See docs/governance/README.md. archive/ and docs/archive/ are history only."
    )

    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(lines),
            }
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"session_start: non-fatal error: {exc}", file=sys.stderr)
        sys.exit(0)

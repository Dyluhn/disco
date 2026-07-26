#!/usr/bin/env python3
"""Stop hook: refuse a voluntary stop that the campaign contract does not allow.

The campaign has exactly one voluntary stop condition: Epics 0-7 all satisfied
and CAMPAIGN-STATUS.md truthfully saying so.  A finding, a hard decision, a
failed test, a provider outage, a finished package, an hourly review, or a
status report is not a stop.

So this hook blocks when either is true:

- an hourly review is overdue, or
- CAMPAIGN-STATUS.md does not declare the completion contract satisfied.

`stop_hook_active` is reported back in the reason rather than used to silently
give up, because giving up is precisely the behaviour the contract forbids.  The
harness's own consecutive-block cap remains the safety valve against a genuinely
stuck loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    CAMPAIGN_STATUS,
    COMPLETION_SENTINEL,
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


def block(reason: str) -> None:
    emit({"decision": "block", "reason": reason})
    sys.exit(0)


def allow() -> None:
    emit({})
    sys.exit(0)


def main() -> int:
    payload = read_hook_input()
    already_blocking = bool(payload.get("stop_hook_active"))
    root = project_dir()
    state = load_state(root)

    reasons: list[str] = []

    if review_is_due(state):
        reasons.append(
            f"• The hourly self-review is overdue by "
            f"{humanize(seconds_until_due(state))}. Append a complete review to "
            f"{SELF_REVIEWS} (every field answered) and update {CAMPAIGN_STATUS}."
        )

    if not completion_contract_satisfied(root):
        reasons.append(
            f"• {CAMPAIGN_STATUS} does not declare the completion contract "
            f"satisfied. It must contain, truthfully, the exact line:\n"
            f'    "{COMPLETION_SENTINEL}"\n'
            "  Until Epics 0-7 each meet every acceptance item, the campaign is not "
            "complete."
        )

    seal_code, seal_output = verify_seal(root)
    if seal_code != 0:
        reasons.append(
            f"• The governance seal is not clean (exit {seal_code}):\n    {seal_output}"
        )

    if not reasons:
        allow()

    detail = "\n".join(reasons)
    nudge = (
        "\n\nThis is a standing stop-hook block, not a new instruction: keep working "
        "the campaign rather than re-asking whether to continue."
        if already_blocking
        else ""
    )
    block(
        "STOP REFUSED — the campaign's voluntary stop condition is not met.\n\n"
        f"{detail}\n\n"
        "Do not end the turn with a question, a menu, 'awaiting direction', or an "
        "offer to proceed. Resolve the item above and continue with the next "
        "concrete campaign action." + nudge
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"stop_gate: non-fatal error: {exc}", file=sys.stderr)
        sys.exit(0)

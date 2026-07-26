#!/usr/bin/env python3
"""Inspect and configure the hourly-review schedule.

Used to functionally prove the mechanism at a short interval and then set it to
the operating value.

    python3 .claude/hooks/review_ctl.py status
    python3 .claude/hooks/review_ctl.py set-interval 60
    python3 .claude/hooks/review_ctl.py set-interval 3600
    python3 .claude/hooks/review_ctl.py force-due      # test-only
    python3 .claude/hooks/review_ctl.py validate       # check the last review block
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _governance import (  # noqa: E402
    CAMPAIGN_STATUS,
    SELF_REVIEWS,
    completion_contract_satisfied,
    humanize,
    last_review_block,
    load_state,
    missing_review_fields,
    project_dir,
    review_is_due,
    save_state,
    seconds_until_due,
    state_path,
    verify_seal,
)


def cmd_status(root: Path) -> int:
    state = load_state(root)
    remaining = seconds_until_due(state)
    seal_code, seal_output = verify_seal(root)
    print(f"state file        : {state_path(root)}")
    print(f"interval          : {state.get('interval_seconds')}s")
    print(f"reviews completed : {state.get('reviews_completed')}")
    last = state.get("last_review_epoch")
    print(f"last review       : {'never' if last is None else time.ctime(int(last))}")
    print(f"next due          : {time.ctime(int(state.get('next_due_epoch', 0)))}")
    due = (
        f"YES (overdue {humanize(remaining)})"
        if review_is_due(state)
        else f"no (in {humanize(remaining)})"
    )
    print(f"due now?          : {due}")
    print(f"seal              : exit {seal_code} {seal_output}")
    contract = "SATISFIED" if completion_contract_satisfied(root) else "not satisfied"
    print(f"completion contract: {contract}")
    return 0


def cmd_set_interval(root: Path, seconds: int) -> int:
    if seconds <= 0:
        print("interval must be positive", file=sys.stderr)
        return 1
    state = load_state(root)
    old = int(state.get("interval_seconds", 0))
    state["interval_seconds"] = seconds
    # Re-anchor the next due time on the new interval from the last review
    # (or from now if none has happened yet).
    anchor = state.get("last_review_epoch") or int(time.time())
    state["next_due_epoch"] = int(anchor) + seconds
    save_state(state, root)
    print(f"interval {old}s -> {seconds}s; next due {time.ctime(state['next_due_epoch'])}")
    return 0


def cmd_force_due(root: Path) -> int:
    state = load_state(root)
    state["next_due_epoch"] = int(time.time()) - 1
    state.pop("last_injection_epoch", None)
    save_state(state, root)
    print("review forced due (test-only)")
    return 0


def cmd_validate(root: Path) -> int:
    block = last_review_block(root)
    missing = missing_review_fields(block)
    if block is None:
        print(f"no review block found in {SELF_REVIEWS}", file=sys.stderr)
        return 1
    if missing:
        print("last review block is INCOMPLETE; missing:", file=sys.stderr)
        for field in missing:
            print(f"  - {field}", file=sys.stderr)
        return 1
    fresh = (root / CAMPAIGN_STATUS).is_file()
    print(f"last review block is COMPLETE; {CAMPAIGN_STATUS} present: {fresh}")
    return 0


def cmd_dump(root: Path) -> int:
    print(json.dumps(load_state(root), indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("force-due")
    sub.add_parser("validate")
    sub.add_parser("dump")
    setter = sub.add_parser("set-interval")
    setter.add_argument("seconds", type=int)
    args = parser.parse_args()

    root = project_dir()
    if args.command == "status":
        return cmd_status(root)
    if args.command == "set-interval":
        return cmd_set_interval(root, args.seconds)
    if args.command == "force-due":
        return cmd_force_due(root)
    if args.command == "validate":
        return cmd_validate(root)
    if args.command == "dump":
        return cmd_dump(root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
